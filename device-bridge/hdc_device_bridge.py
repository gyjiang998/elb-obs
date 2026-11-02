#!/usr/bin/env python3
"""Windows/macOS/Linux HDC Device Bridge.

This is intentionally a small process, not an Agent.  It connects *outbound*
to the Platform API, validates backend-signed ``device_job/v1`` envelopes, and
starts only a configured ``hdc(.exe)`` through argv-based subprocess APIs.
It never accepts a workstation shell command from a model or from a job.

Typical Windows setup::

    py -m pip install -r requirements.txt
    py hdc_device_bridge.py pair --platform-url http://platform.example:4032 \
       --hdc C:\\OH\\toolchains\\hdc.exe
    py hdc_device_bridge.py run --config %LOCALAPPDATA%\\HeteroBridge\\bridge.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener

try:
    import websockets
except ImportError as exc:  # pragma: no cover - deployment dependency
    raise SystemExit("缺少 websockets。请先执行: py -m pip install -r requirements.txt") from exc


WEBSOCKET_PROTOCOL_VERSION = "device-bridge-ws/v1"
HTTP_POLL_PROTOCOL_VERSION = "device-bridge-http-poll/v1"
PROTOCOL_VERSION = HTTP_POLL_PROTOCOL_VERSION
BRIDGE_IMPLEMENTATION_VERSION = "1.4.9"
DEPLOYMENT_CONTRACT_VERSION = "device-deployment/v2"
DIAGNOSTICS_SCHEMA_VERSION = "device_bridge_diagnostics/v1"
DIAGNOSTICS_REFRESH_SECONDS = 30
CONSOLE_HEARTBEAT_SECONDS = 60
MAX_OUTPUT_BYTES = 64 * 1024
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_COLLECT_BYTES = 2 * 1024 * 1024 * 1024
MAX_JOB_SECONDS = 900
BRIDGE_ACTIONS = (
    "device.discover",
    "device.probe",
    "device.deploy_artifact",
    "device.execute_shell",
    "device.collect_artifact",
    "device.cancel_job",
    "trace.library.list",
    "trace.library.delete",
)
SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
REMOTE_PATH_PATTERN = re.compile(r"^/data/local/tmp/[A-Za-z0-9._/-]{1,360}$")


def _text(value: Any, limit: int = 16 * 1024) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _safe_serial(value: Any) -> str:
    serial = _text(value, 160)
    if not SERIAL_PATTERN.fullmatch(serial):
        raise ValueError("device serial invalid")
    return serial


def _safe_remote_path(value: Any) -> str:
    path = _text(value, 512)
    if not REMOTE_PATH_PATTERN.fullmatch(path) or "/../" in path or path.endswith("/.."):
        raise ValueError("remote path invalid")
    return path


def _remote_parent(value: Any) -> str:
    """Return a device path parent without inheriting the Bridge host OS."""

    return str(PurePosixPath(_safe_remote_path(value)).parent)


def _parse_deadline(value: Any) -> float:
    try:
        return datetime.fromisoformat(_text(value, 80).replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise ValueError("job deadline invalid") from exc


def _bounded(value: bytes | str, limit: int = MAX_OUTPUT_BYTES) -> str:
    raw = value.encode("utf-8", errors="replace") if isinstance(value, str) else value
    marker = b"\n[output truncated]"
    if len(raw) > limit:
        raw = raw[: max(0, limit - len(marker))] + marker
    return raw.decode("utf-8", errors="replace")


class PlatformHttpError(RuntimeError):
    def __init__(self, *, url: str, status_code: int, error_code: str, message: str, response_preview: str) -> None:
        self.url = url
        self.status_code = status_code
        self.error_code = error_code
        self.response_preview = response_preview
        endpoint = urlparse(url).path or "/"
        detail = f"{error_code}: {message}" if error_code else message or response_preview
        super().__init__(f"POST {endpoint} returned HTTP {status_code}{f': {detail}' if detail else ''}")


class PlatformTransportError(RuntimeError):
    """Describe a failed exchange without logging credentials or request data."""

    def __init__(self, *, url: str, phase: str, timeout: float, elapsed_ms: float, error: Exception) -> None:
        parsed = urlparse(url)
        hostname = parsed.hostname or "unknown-host"
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        self.endpoint = f"{parsed.scheme}://{host}{port}{parsed.path or '/'}"
        self.phase = phase
        self.timeout_seconds = timeout
        self.elapsed_ms = elapsed_ms
        reason = getattr(error, "reason", None)
        self.exception_type = type(reason if isinstance(reason, Exception) else error).__name__
        super().__init__(
            f"POST {self.endpoint} transport failed: phase={phase}, "
            f"timeout={timeout:g}s, elapsed_ms={elapsed_ms:.1f}, exception={self.exception_type}"
        )


def _direct_urlopen(request: Request, *, timeout: float) -> Any:
    """Open the configured Platform directly, ignoring ambient host proxies."""

    return build_opener(ProxyHandler({})).open(request, timeout=timeout)  # nosec B310: administrator configured origin


def _http_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: int = 30,
    idempotency_key: str = "",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "User-Agent": "Heterogeneous-HDC-Device-Bridge/1.0"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if extra_headers:
        headers.update(extra_headers)
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.monotonic()
    phase = "connect_or_response_headers"
    try:
        with _direct_urlopen(request, timeout=timeout) as response:
            phase = "response_body"
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            raw = exc.read(64 * 1024)
        finally:
            exc.close()
        preview = _bounded(raw, 4096)
        try:
            payload_value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload_value = {}
        error = _record(_record(payload_value).get("error"))
        raise PlatformHttpError(
            url=url,
            status_code=int(exc.code),
            error_code=_text(error.get("code"), 160),
            message=_text(error.get("message"), 1200),
            response_preview=preview,
        ) from exc
    except OSError as exc:
        raise PlatformTransportError(
            url=url,
            phase=phase,
            timeout=timeout,
            elapsed_ms=(time.monotonic() - started) * 1000,
            error=exc,
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("platform returned invalid JSON")
    return value


def _validated_platform_url(value: str) -> str:
    normalized = _text(value, 512).rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("platform URL must be an absolute http(s) origin without query or fragment")
    return normalized


def _safe_platform_relative_url(value: str) -> str:
    parsed = urlparse(_text(value, 2048))
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        raise ValueError("Bridge artifact URL must be a platform-relative path")
    return parsed.geturl()


def _direct_websocket_options() -> dict[str, Any]:
    """Keep a configured LAN platform origin out of ambient host proxies."""

    return {"proxy": None} if "proxy" in inspect.signature(websockets.connect).parameters else {}


class _QueuedHttpEventTransport:
    """WebSocket-shaped sender backed by the Bridge's durable HTTP queue."""

    def __init__(self, bridge: "HdcDeviceBridge") -> None:
        self.bridge = bridge

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ValueError("Bridge event must be a JSON object")
        self.bridge._queue_http_event(message)


class HdcDeviceBridge:
    def __init__(self, config: dict[str, Any], config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.bridge_id = _text(config.get("bridge_id"), 80)
        self.bridge_secret = _text(config.get("bridge_secret"), 200)
        self.platform_http_url = _validated_platform_url(_text(config.get("platform_http_url"), 512))
        self.transport = _text(config.get("transport"), 40) or "http_poll"
        if self.transport not in {"http_poll", "websocket"}:
            raise ValueError("Device Bridge transport must be http_poll or websocket")
        self.platform_ws_url = _text(config.get("platform_ws_url"), 512).rstrip("/")
        if not self.platform_ws_url:
            ws_scheme = "wss" if self.platform_http_url.startswith("https://") else "ws"
            self.platform_ws_url = ws_scheme + "://" + self.platform_http_url.split("://", 1)[1] + "/v1/device-bridges/ws"
        parsed_ws = urlparse(self.platform_ws_url)
        if parsed_ws.scheme not in {"wss", "ws"} or not parsed_ws.netloc:
            raise ValueError("Device Bridge platform WebSocket URL must be an absolute ws(s) URL")
        self.hdc_path = Path(_text(config.get("hdc_path"), 1024)).expanduser().resolve()
        self.spool_root = Path(_text(config.get("spool_root"), 1024) or str(config_path.parent / "spool")).expanduser().resolve()
        self.spool_root.mkdir(parents=True, exist_ok=True)
        self.trace_root = Path(_text(config.get("trace_root"), 1024) or str(config_path.parent / "traces")).expanduser().resolve()
        if self.trace_root.is_relative_to(self.spool_root):
            raise ValueError("persistent trace_root must be outside the transport spool")
        self._trace_lock = asyncio.Lock()
        self._active_trace_ids: set[str] = set()
        self.job_spool_root = self.spool_root / "jobs"
        self.job_spool_root.mkdir(parents=True, exist_ok=True)
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.cancelled_jobs: set[str] = set()
        self.jobs: dict[str, dict[str, Any]] = self._load_spooled_jobs()
        self._http_events: deque[dict[str, Any]] = deque()
        self._http_replay_event_ids: set[int] = set()
        self._http_in_flight_event_ids: set[int] = set()
        self._reported_result_job_ids: set[str] = set()
        self._job_tasks: dict[str, asyncio.Task[None]] = {}
        self._job_deadlines: dict[str, float] = {}
        self._job_cancel_events: dict[str, asyncio.Event] = {}
        self._hdc_finished: dict[str, asyncio.Event] = {}
        self._hdc_lock = asyncio.Lock()
        self._runtime_diagnostics: dict[str, Any] = {}
        self._diagnostics_checked_at_monotonic = 0.0

    def _job_path(self, job_id: str) -> Path:
        return self.job_spool_root / f"{job_id}.json"

    def _load_spooled_jobs(self) -> dict[str, dict[str, Any]]:
        jobs: dict[str, dict[str, Any]] = {}
        for path in self.job_spool_root.glob("devjob_*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                job_id = _text(value.get("job_id"), 80)
                if job_id:
                    jobs[job_id] = value
        return jobs

    def _persist_job(self, job_id: str, value: dict[str, Any]) -> None:
        safe_id = _text(job_id, 80)
        if not safe_id:
            return
        target = self._job_path(safe_id)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, target)
        self.jobs[safe_id] = value

    def _known_jobs(self) -> list[dict[str, Any]]:
        return [
            {
                "job_id": job_id,
                "request_id": item.get("request_id"),
                "idempotency_key": item.get("idempotency_key"),
                "state": item.get("state"),
            }
            for job_id, item in sorted(self.jobs.items())
        ][-256:]

    def capabilities(self) -> dict[str, Any]:
        capabilities = {
            "protocol_version": HTTP_POLL_PROTOCOL_VERSION if self.transport == "http_poll" else WEBSOCKET_PROTOCOL_VERSION,
            "implementation_version": BRIDGE_IMPLEMENTATION_VERSION,
            "transport": self.transport,
            "deployment_contract_version": DEPLOYMENT_CONTRACT_VERSION,
            "platform": sys.platform,
            "hdc_path_configured": self.hdc_path.is_file(),
            "actions": list(BRIDGE_ACTIONS),
            "discovery_profiles": ["default", "targets_only"],
            "trace_library": {"version": "trace_library/v1", "persistent_raw_files": True},
        }
        if self._runtime_diagnostics:
            capabilities["runtime_diagnostics"] = self._runtime_diagnostics
        return capabilities

    def _bridge_headers(self) -> dict[str, str]:
        return {
            "X-Bridge-Id": self.bridge_id,
            "X-Bridge-Secret": self.bridge_secret,
        }

    def _queue_http_event(self, message: dict[str, Any], *, replay: bool = False) -> None:
        kind = _text(message.get("type"), 80)
        job_id = _text(message.get("job_id"), 80)
        if kind == "job.result":
            if job_id in self._reported_result_job_ids:
                return
            duplicate = next((item for item in self._http_events
                if _text(item.get("type"), 80) == "job.result"
                and _text(item.get("job_id"), 80) == job_id), None)
            if duplicate is not None:
                if not replay:
                    # An explicitly re-offered job may already have a result
                    # awaiting background replay. Give its live response the
                    # same priority without transmitting a duplicate.
                    self._http_replay_event_ids.discard(id(duplicate))
                return
        if len(self._http_events) >= 1024:
            stale_progress = next(
                (item for item in self._http_events if _text(item.get("type"), 80) == "job.progress"),
                None,
            )
            if stale_progress is not None:
                self._http_events.remove(stale_progress)
            elif not replay and (old_result := next((item for item in self._http_events
                    if id(item) in self._http_replay_event_ids
                    and id(item) not in self._http_in_flight_event_ids), None)) is not None:
                # A full history spool must not exclude a newly completed
                # discovery result. Evicted history remains durable on disk.
                self._http_events.remove(old_result)
                self._http_replay_event_ids.discard(id(old_result))
            elif kind in {"job.progress", "job.accepted", "bridge.heartbeat"}:
                return
            else:
                # Completed results remain in the disk spool and are queued
                # again after earlier events have been acknowledged.
                return
        queued = dict(message)
        self._http_events.append(queued)
        if replay:
            self._http_replay_event_ids.add(id(queued))

    def _queue_completed_results(self) -> None:
        for job_id, item in list(self.jobs.items()):
            envelope = item.get("result_envelope")
            delivery = _record(item.get("result_delivery"))
            if job_id in self._reported_result_job_ids or delivery.get("state") == "quarantined" or not isinstance(envelope, dict):
                continue
            envelope_bridge_id = _text(envelope.get("bridge_id"), 80)
            if envelope_bridge_id and envelope_bridge_id != self.bridge_id:
                self._quarantine_http_event(
                    envelope,
                    reason_code="BRIDGE_SPOOL_IDENTITY_MISMATCH",
                    detail="完成结果属于另一 Bridge identity，未向当前后端回放。",
                )
                continue
            self._queue_http_event(envelope, replay=True)

    def _quarantine_http_event(self, message: dict[str, Any], *, reason_code: str, detail: str) -> None:
        event_type = _text(message.get("type"), 80)
        job_id = _text(message.get("job_id"), 80)
        observation = {
            "quarantined_at": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "job_id": job_id or None,
            "reason_code": reason_code,
            "detail": detail[:1200],
        }
        if event_type == "job.result" and job_id:
            self._reported_result_job_ids.add(job_id)
            item = dict(_record(self.jobs.get(job_id)))
            if item:
                item["result_delivery"] = {"state": "quarantined", **observation}
                self._persist_job(job_id, item)
        try:
            audit_path = self.spool_root / "quarantined-events.jsonl"
            with audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(observation, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            print(f"Device Bridge could not persist quarantine audit: {str(exc)[:300]}", file=sys.stderr)
        print(json.dumps({"status": "bridge_event_quarantined", **observation}, ensure_ascii=False), file=sys.stderr)

    async def _flush_http_events(self, connection_id: str, *, limit: int = 64,
                                 replay: bool | None = None) -> None:
        event_url = urljoin(self.platform_http_url + "/", "v1/device-bridges/events")
        sent = 0
        while self._http_events and sent < max(1, min(limit, 64)):
            candidates = [item for item in self._http_events
                if id(item) not in self._http_in_flight_event_ids
                and (replay is None or (id(item) in self._http_replay_event_ids) == replay)]
            if not candidates:
                break
            message = candidates[0]
            self._http_in_flight_event_ids.add(id(message))
            try:
                try:
                    response = await asyncio.to_thread(
                        _http_json,
                        event_url,
                        {"connection_id": connection_id, "message": message},
                        timeout=30,
                        extra_headers=self._bridge_headers(),
                    )
                finally:
                    self._http_in_flight_event_ids.discard(id(message))
            except PlatformHttpError as exc:
                if exc.error_code in {
                    "BRIDGE_JOB_RESULT_STALE",
                    "BRIDGE_JOB_RESULT_UNAUTHORIZED",
                    "BRIDGE_JOB_PROGRESS_STALE",
                    "BRIDGE_JOB_PROGRESS_UNAUTHORIZED",
                    "BRIDGE_JOB_ACCEPTED_STALE",
                    "BRIDGE_JOB_ACCEPTED_UNAUTHORIZED",
                }:
                    self._ack_http_event(message)
                    self._quarantine_http_event(
                        message,
                        reason_code=exc.error_code,
                        detail=str(exc),
                    )
                    sent += 1
                    continue
                raise
            if response.get("acknowledged") is not True:
                raise RuntimeError("platform did not acknowledge the Bridge event")
            self._ack_http_event(message)
            if response.get("event_disposition") == "stale_ignored":
                self._quarantine_http_event(
                    message,
                    reason_code=_text(response.get("reason_code"), 160) or "BRIDGE_EVENT_STALE",
                    detail="后端已幂等确认并忽略该过期 Bridge 事件。",
                )
                sent += 1
                continue
            if _text(message.get("type"), 80) == "job.result":
                job_id = _text(message.get("job_id"), 80)
                if job_id:
                    self._reported_result_job_ids.add(job_id)
                    item = dict(_record(self.jobs.get(job_id)))
                    if item:
                        item["result_delivery"] = {"state": "acknowledged",
                            "acknowledged_at": datetime.now(UTC).isoformat(),
                            "platform_http_url": self.platform_http_url,
                            "bridge_id": self.bridge_id}
                        self._persist_job(job_id, item)
            sent += 1

    def _ack_http_event(self, message: dict[str, Any]) -> None:
        # A full queue may shed a progress event while its POST is in flight.
        # Acknowledge only that exact envelope, never the subsequent result.
        for index, queued in enumerate(self._http_events):
            if queued is message:
                del self._http_events[index]
                self._http_replay_event_ids.discard(id(message))
                break

    def _schedule_job(self, transport: Any, job: dict[str, Any]) -> None:
        job_id = _text(job.get("job_id"), 80)
        active = self._job_tasks.get(job_id)
        if active is not None and not active.done():
            return
        task = asyncio.create_task(self._execute_job(transport, job))
        self._job_tasks[job_id] = task

        def completed(finished: asyncio.Task[None]) -> None:
            if self._job_tasks.get(job_id) is finished:
                self._job_tasks.pop(job_id, None)
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"Device Bridge job task failed: {str(exc)[:500]}", file=sys.stderr)

        task.add_done_callback(completed)

    def _spool_diagnostics(self) -> dict[str, Any]:
        marker = self.spool_root / f".bridge-write-check-{os.getpid()}"
        try:
            marker.write_bytes(b"bridge-spool-ok")
            observed = marker.read_bytes()
            return {
                "writable": observed == b"bridge-spool-ok",
                "path_hint": self.spool_root.name,
                "error": None,
            }
        except OSError as exc:
            return {
                "writable": False,
                "path_hint": self.spool_root.name,
                "error": f"{type(exc).__name__}: {str(exc)[:600]}",
            }
        finally:
            marker.unlink(missing_ok=True)

    @staticmethod
    def _target_serials(result: dict[str, Any]) -> list[str]:
        if result.get("return_code") != 0:
            return []
        serials: list[str] = []
        for line in _text(result.get("stdout"), MAX_OUTPUT_BYTES).splitlines():
            serial = line.strip().split()[0] if line.strip() else ""
            if SERIAL_PATTERN.fullmatch(serial):
                serials.append(serial)
        return serials

    async def _refresh_runtime_diagnostics(
        self,
        *,
        targets_result: dict[str, Any] | None = None,
        force: bool = False,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            not force
            and self._runtime_diagnostics
            and time.monotonic() - self._diagnostics_checked_at_monotonic < DIAGNOSTICS_REFRESH_SECONDS
        ):
            return self._runtime_diagnostics

        hdc_exists = self.hdc_path.is_file()
        if hdc_exists:
            version = await self._run_hdc("-v", timeout_seconds=20, job_id=job_id)
            if version.get("return_code") != 0:
                fallback_version = await self._run_hdc("version", timeout_seconds=20, job_id=job_id)
                if fallback_version.get("return_code") == 0:
                    version = fallback_version
                else:
                    version["fallback"] = fallback_version
            targets = targets_result or await self._run_hdc("list", "targets", timeout_seconds=30, job_id=job_id)
        else:
            unavailable = {
                "return_code": None,
                "timed_out": False,
                "cancelled": False,
                "stdout": "",
                "stderr": "configured hdc executable is unavailable",
                "duration_ms": 0.0,
            }
            version = dict(unavailable)
            targets = targets_result or dict(unavailable)
        serials = self._target_serials(targets)
        spool = self._spool_diagnostics()
        issues: list[dict[str, str]] = []
        if not hdc_exists:
            issues.append({
                "code": "BRIDGE_HDC_PATH_UNAVAILABLE",
                "failure_domain": "windows_bridge_configuration",
                "message_zh": "Bridge 配置中的 hdc.exe 绝对路径不存在；Windows PATH 中的另一份 hdc 不会被自动采用。",
            })
        elif version.get("return_code") != 0:
            issues.append({
                "code": "BRIDGE_HDC_VERSION_FAILED",
                "failure_domain": "windows_hdc_process",
                "message_zh": "Bridge 能找到 hdc.exe，但执行版本探针失败。",
            })
        if targets.get("return_code") != 0:
            issues.append({
                "code": "BRIDGE_HDC_TARGET_LIST_FAILED",
                "failure_domain": "windows_hdc_process",
                "message_zh": "Bridge 执行 hdc list targets 失败。",
            })
        elif not serials:
            issues.append({
                "code": "BRIDGE_DEVICE_NOT_FOUND",
                "failure_domain": "windows_hdc_device_connection",
                "message_zh": "Bridge 当前使用的 hdc.exe 没有发现设备。",
            })
        if not spool.get("writable"):
            issues.append({
                "code": "BRIDGE_SPOOL_NOT_WRITABLE",
                "failure_domain": "windows_spool",
                "message_zh": "Bridge 临时产物目录不可写。",
            })
        fatal_codes = {
            "BRIDGE_HDC_PATH_UNAVAILABLE",
            "BRIDGE_HDC_VERSION_FAILED",
            "BRIDGE_HDC_TARGET_LIST_FAILED",
            "BRIDGE_SPOOL_NOT_WRITABLE",
        }
        diagnostics = {
            "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
            "checked_at": datetime.now(UTC).isoformat(),
            "status": (
                "failed"
                if any(item["code"] in fatal_codes for item in issues)
                else "attention"
                if issues
                else "ready"
            ),
            "bridge": {
                "implementation_version": BRIDGE_IMPLEMENTATION_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "deployment_contract_version": DEPLOYMENT_CONTRACT_VERSION,
                "host_platform": sys.platform,
            },
            "hdc": {
                "configured_path_exists": hdc_exists,
                "executable_name": self.hdc_path.name,
                "configured_parent_hint": self.hdc_path.parent.name,
                "version": version,
                "targets": targets,
                "target_count": len(serials),
            },
            "spool": spool,
            "issues": issues,
        }
        self._runtime_diagnostics = diagnostics
        self._diagnostics_checked_at_monotonic = time.monotonic()
        return diagnostics

    def _verify_job(self, job: dict[str, Any]) -> None:
        if job.get("schema_version") != "device_job/v1":
            raise ValueError("unsupported device job schema")
        if _text(job.get("bridge_id"), 80) != self.bridge_id:
            raise ValueError("job belongs to another bridge")
        if _parse_deadline(job.get("deadline")) <= time.time():
            raise ValueError("job deadline expired")
        unsigned = dict(job)
        signature = _text(unsigned.pop("signature", ""), 128)
        expected = hmac.new(self.bridge_secret.encode("utf-8"), _canonical_json(unsigned), hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            raise ValueError("job signature invalid")
        if _text(job.get("action"), 120) not in self.capabilities()["actions"]:
            raise ValueError("job action unsupported")

    async def _run_hdc(self, *args: str, timeout_seconds: float, job_id: str | None = None,
                       cancel_immediately: bool = False) -> dict[str, Any]:
        """Budget queueing and execution together, checking again before spawn."""

        started = time.monotonic()
        deadline = min(started + max(0.0, min(float(timeout_seconds), MAX_JOB_SECONDS)),
                       self._job_deadlines.get(job_id or "", float("inf")))

        def stopped(*, cancelled: bool = False) -> dict[str, Any]:
            return {"return_code": None, "timed_out": not cancelled, "cancelled": cancelled,
                    "stdout": "", "stderr": "HDC job cancelled while queued" if cancelled else "HDC deadline expired while waiting for command slot",
                    "error_code": "BRIDGE_HDC_CANCELLED" if cancelled else "BRIDGE_HDC_QUEUE_TIMEOUT",
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "invocation": self._hdc_invocation(args)}

        if job_id and job_id in self.cancelled_jobs:
            return stopped(cancelled=True)
        if deadline <= started:
            return stopped()
        finished = asyncio.Event()
        if job_id:
            self._hdc_finished[job_id] = finished
        acquire = asyncio.create_task(self._hdc_lock.acquire())
        cancellation = asyncio.create_task(self._job_cancel_events.setdefault(job_id, asyncio.Event()).wait()) if job_id else None
        waiters = [acquire, *([cancellation] if cancellation is not None else [])]
        try:
            await asyncio.wait(waiters, timeout=max(0.0, deadline - time.monotonic()), return_when=asyncio.FIRST_COMPLETED)
            if job_id and job_id in self.cancelled_jobs:
                return stopped(cancelled=True)
            remaining = deadline - time.monotonic()
            if not acquire.done() or remaining <= 0:
                return stopped()
            result = await self._run_hdc_unlocked(*args, timeout_seconds=remaining, job_id=job_id,
                                                cancel_immediately=cancel_immediately)
            result["queue_duration_ms"] = round(max(0.0, (time.monotonic() - started) * 1000 - float(result.get("duration_ms", 0))), 3)
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            return result
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            # Cancellation may race with a successful lock acquisition. Only
            # this acquire task's result establishes ownership of the lock.
            if acquire.done() and not acquire.cancelled() and acquire.exception() is None and acquire.result():
                self._hdc_lock.release()
            finished.set()
            if job_id and self._hdc_finished.get(job_id) is finished:
                self._hdc_finished.pop(job_id, None)

    def _hdc_invocation(self, args: tuple[str, ...]) -> dict[str, Any]:
        display_args: list[str] = []
        for value in args:
            try:
                relative = Path(value).expanduser().resolve().relative_to(self.spool_root)
                display_args.append(f"<bridge-spool>/{relative.as_posix()}")
            except (OSError, ValueError):
                display_args.append(value)
        return {"executable_name": self.hdc_path.name, "arguments": display_args}

    def _operation_deadline(self, job: dict[str, Any], timeout_seconds: float) -> float:
        now = time.monotonic()
        deadline = min(now + max(0.0, min(timeout_seconds, MAX_JOB_SECONDS)),
                       self._job_deadlines.get(str(job["job_id"]), float("inf")))
        if job.get("deadline"):
            deadline = min(deadline, now + _parse_deadline(job["deadline"]) - time.time())
        return deadline

    async def _run_hdc_unlocked(self, *args: str, timeout_seconds: float, job_id: str | None = None,
                                cancel_immediately: bool = False) -> dict[str, Any]:
        if not self.hdc_path.is_file():
            raise RuntimeError("configured hdc executable is unavailable")
        started = time.monotonic()
        deadline = min(started + max(0.0, min(float(timeout_seconds), MAX_JOB_SECONDS)),
                       self._job_deadlines.get(job_id or "", float("inf")))
        invocation = self._hdc_invocation(args)
        if job_id and job_id in self.cancelled_jobs:
            return {"return_code": None, "timed_out": False, "cancelled": True, "stdout": "", "stderr": "HDC job cancelled before command start", "duration_ms": 0.0, "invocation": invocation}
        if deadline <= time.monotonic():
            return {"return_code": None, "timed_out": True, "cancelled": False, "stdout": "", "stderr": "HDC deadline expired before command start", "duration_ms": 0.0, "invocation": invocation}

        async def create_owned_process() -> asyncio.subprocess.Process | None:
            # The event loop can run other work between scheduling this task
            # and actually entering the subprocess API.
            if time.monotonic() >= deadline or (job_id and job_id in self.cancelled_jobs):
                return None
            return await asyncio.create_subprocess_exec(
                str(self.hdc_path), *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )

        try:
            creation = asyncio.create_task(create_owned_process())
            try:
                done, _ = await asyncio.wait((creation,), timeout=max(0.0, deadline - time.monotonic()))
                if not done:
                    raise TimeoutError
                process = creation.result()
            except (asyncio.CancelledError, TimeoutError) as interrupted:
                # Process creation can race with timeout/cancel. Resolve its
                # handle and reap that child before releasing the HDC slot.
                try:
                    process = await creation
                except OSError:
                    if isinstance(interrupted, asyncio.CancelledError):
                        raise interrupted
                    raise
                if process is not None:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    await process.wait()
                if isinstance(interrupted, asyncio.CancelledError):
                    raise interrupted
                return {"return_code": None, "timed_out": True, "cancelled": False, "stdout": "", "stderr": "HDC deadline reached during process start", "duration_ms": round((time.monotonic() - started) * 1000, 3), "invocation": invocation}
        except OSError as exc:
            return {
                "return_code": None,
                "timed_out": False,
                "cancelled": False,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {str(exc)[:1200]}",
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "invocation": invocation,
                "launch_error": {
                    "code": "BRIDGE_HDC_PROCESS_START_FAILED",
                    "message": str(exc)[:1200],
                },
            }
        if process is None:
            cancelled = bool(job_id and job_id in self.cancelled_jobs)
            return {"return_code": None, "timed_out": not cancelled, "cancelled": cancelled, "stdout": "", "stderr": "HDC command stopped before process creation", "duration_ms": round((time.monotonic() - started) * 1000, 3), "invocation": invocation}
        if job_id:
            self.processes[job_id] = process
        try:
            if job_id and job_id in self.cancelled_jobs:
                process.kill()
                await process.wait()
                return {"return_code": None, "timed_out": False, "cancelled": True, "stdout": "", "stderr": "HDC job cancelled during process start", "duration_ms": round((time.monotonic() - started) * 1000, 3), "invocation": invocation}
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(0.0, deadline - time.monotonic()))
        except asyncio.CancelledError:
            if process.returncode is None:
                if cancel_immediately or time.monotonic() >= deadline:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                else:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
            raise
        except TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            return {"return_code": None, "timed_out": True, "cancelled": False, "stdout": "", "stderr": "HDC command timed out", "duration_ms": round((time.monotonic() - started) * 1000, 3), "invocation": invocation}
        finally:
            if job_id:
                self.processes.pop(job_id, None)
        return {
            "return_code": process.returncode,
            "timed_out": False,
            "cancelled": bool(job_id and job_id in self.cancelled_jobs),
            "stdout": _bounded(stdout),
            "stderr": _bounded(stderr),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "invocation": invocation,
        }

    async def _discover(self, job: dict[str, Any]) -> dict[str, Any]:
        targets_only = _record(job.get("payload")).get("targets_only") is True
        if targets_only:
            started = time.monotonic()
            remaining = min(10.0, _parse_deadline(job.get("deadline")) - time.time())
            try:
                if remaining <= 0:
                    raise TimeoutError
                # Include queueing behind the Bridge-wide HDC lock in the
                # signed deadline; cancellation kills/reaps only this job's
                # read-only HDC client before returning.
                result = await asyncio.wait_for(self._run_hdc("list", "targets", timeout_seconds=remaining,
                    job_id=str(job["job_id"]), cancel_immediately=True), timeout=remaining)
            except TimeoutError:
                result = {"return_code": None, "timed_out": True, "cancelled": False, "stdout": "",
                    "stderr": "Device discovery deadline reached while waiting for HDC or listing targets",
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "error_code": "BRIDGE_DISCOVERY_DEADLINE"}
        else:
            result = await self._run_hdc("list", "targets", timeout_seconds=30, job_id=str(job["job_id"]))
        devices = []
        if result["return_code"] == 0:
            for line in result["stdout"].splitlines():
                serial = line.strip().split()[0] if line.strip() else ""
                if SERIAL_PATTERN.fullmatch(serial):
                    devices.append({
                        "logical_device_id": f"device_{hashlib.sha256(serial.encode()).hexdigest()[:24]}",
                        # The raw serial remains in the signed Bridge result so
                        # a later probe can target the same device. The platform
                        # treats it as an execution reference, not a public
                        # hardware capability claim.
                        "device_serial": serial,
                        "connection_state": "connected",
                    })
        diagnostics = self._runtime_diagnostics
        if not targets_only and not result.get("timed_out") and not result.get("cancelled"):
            diagnostics = await self._refresh_runtime_diagnostics(targets_result=result, force=True, job_id=str(job["job_id"]))
        return {
            "status": ("cancelled" if result.get("cancelled") else
                       "timed_out" if result.get("timed_out") else
                       "succeeded" if result["return_code"] == 0 else "failed"),
            "devices": devices,
            "hdc": result,
            "bridge_diagnostics": diagnostics,
            **({"discovery_profile": "targets_only", "bridge_diagnostics_source": "cached" if diagnostics else "unavailable"} if targets_only else {}),
        }

    async def _probe(self, websocket: Any, job: dict[str, Any]) -> dict[str, Any]:
        payload = _record(job.get("payload"))
        serial = _safe_serial(payload.get("device_serial"))
        # Fixed, read-only command set.  Job data cannot choose property names.
        property_keys = {
            "model": "const.product.model",
            "hardware": "ohos.boot.hardware",
            "chipset": "ohos.boot.chiptype",
            "os_full_name": "const.ohos.fullname",
            "api_version": "const.ohos.apiversion",
            "gpu_vendor": "const.gpu.vendor",
        }
        paths = {
            "gpu": ["/system/lib64/libEGL.so", "/system/lib64/libGLESv3.so", "/system/lib64/libvulkan.so"],
            "npu": ["/system/lib64/ndk/libneural_network_runtime.so", "/system/lib64/ndk/libneural_network_core.so"],
            "dsp": ["/vendor/lib64/libdsp_ctl.so"],
            "media": ["/system/lib64/libnative_media_core.so", "/system/lib64/libnative_media_codecbase.so"],
        }
        property_marker = "__HETERO_PROPERTY__"
        path_marker = "__HETERO_RUNTIME_PATH__"
        commands = [
            f'printf "{property_marker}{name}=%s\\n" "$(param get {key} 2>/dev/null)"'
            for name, key in property_keys.items()
        ]
        commands.extend(
            f'if [ -e "{path}" ]; then printf "{path_marker}{unit}=%s\\n" "{path}"; fi'
            for unit, candidates in paths.items()
            for path in candidates
        )
        await self._send_job_progress(
            websocket,
            job,
            stage="device_probe_batch_started",
            detail_zh="Windows Bridge 正在执行单次只读设备能力探测。",
            property_count=len(property_keys),
            runtime_path_count=sum(len(items) for items in paths.values()),
        )
        output = await self._run_hdc(
            "-t",
            serial,
            "shell",
            "; ".join(commands),
            timeout_seconds=30,
            job_id=str(job["job_id"]),
        )
        if output.get("return_code") != 0:
            await self._send_job_progress(
                websocket,
                job,
                stage="device_probe_batch_failed",
                detail_zh="设备能力探测未完成，已返回真实 HDC 诊断。",
                timed_out=bool(output.get("timed_out")),
                duration_ms=output.get("duration_ms"),
            )
            return {
                "status": "failed",
                "schema_version": "device_probe/v1",
                "stage": "device_probe_batch",
                "failure_domain": "windows_hdc_to_device_transport",
                "device": {"serial": serial},
                "hdc": output,
                "error": {
                    "code": "DEVICE_PROBE_HDC_FAILED",
                    "message": "单次只读 HDC 设备探测失败或超时。",
                },
            }

        values: dict[str, str | None] = {name: None for name in property_keys}
        observed_paths: dict[str, list[str]] = {unit: [] for unit in paths}
        for line in _text(output.get("stdout"), MAX_OUTPUT_BYTES).splitlines():
            if line.startswith(property_marker):
                name, separator, value = line[len(property_marker):].partition("=")
                if separator and name in values:
                    values[name] = value.strip() or None
            elif line.startswith(path_marker):
                unit, separator, path = line[len(path_marker):].partition("=")
                if separator and unit in observed_paths and path in paths[unit]:
                    observed_paths[unit].append(path)

        observed_units: dict[str, Any] = {}
        for unit in paths:
            observed = observed_paths[unit]
            observed_units[unit] = {
                "unit_id": unit,
                "observation_status": "observed" if observed else "not_observed",
                "observed_runtime_paths": observed,
                "claim_boundary_zh": "Bridge probe 是只读运行时观察，不等价于目标热点已卸载或存在性能收益。",
            }
        await self._send_job_progress(
            websocket,
            job,
            stage="device_probe_batch_completed",
            detail_zh="单次只读设备能力探测已完成。",
            duration_ms=output.get("duration_ms"),
            observed_unit_count=sum(1 for item in observed_units.values() if item["observation_status"] == "observed"),
        )
        return {
            "status": "succeeded",
            "schema_version": "device_probe/v1",
            "device": {"serial": serial, **values},
            "observed_units": observed_units,
            "hdc": output,
            "probe_profile": "bridge-hdc-readonly-capability-probe/v2",
        }

    async def _send_job_progress(
        self,
        websocket: Any,
        job: dict[str, Any],
        *,
        stage: str,
        detail_zh: str,
        **facts: Any,
    ) -> None:
        await websocket.send(json.dumps({
            "type": "job.progress",
            "job_id": job["job_id"],
            "request_id": job.get("request_id"),
            "progress": {"stage": stage, "action": job.get("action"), "detail_zh": detail_zh, **facts},
        }, ensure_ascii=False))

    async def _download_artifact(self, job: dict[str, Any], url: str, expected_sha: str) -> tuple[Path, dict[str, Any]]:
        destination = self.spool_root / str(job["job_id"]) / "artifact.bin"
        destination.parent.mkdir(parents=True, exist_ok=True)

        def download() -> dict[str, Any]:
            started = time.monotonic()
            digest = hashlib.sha256()
            resolved_url = urljoin(self.platform_http_url + "/", _safe_platform_relative_url(url).lstrip("/"))
            request = Request(
                resolved_url,
                headers={
                    "User-Agent": "Heterogeneous-HDC-Device-Bridge/1.0",
                    "X-Bridge-Id": self.bridge_id,
                    "X-Bridge-Secret": self.bridge_secret,
                },
            )
            total = 0
            try:
                with _direct_urlopen(request, timeout=120) as response, destination.open("wb") as handle:
                    declared_sha = _text(response.headers.get("X-Artifact-Sha256"), 80)
                    if declared_sha and declared_sha != expected_sha:
                        raise RuntimeError("platform artifact sha256 header mismatch")
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise RuntimeError("build artifact exceeds bridge download limit")
                        digest.update(chunk)
                        handle.write(chunk)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            observed_sha = digest.hexdigest()
            if observed_sha != expected_sha:
                destination.unlink(missing_ok=True)
                raise RuntimeError("build artifact sha256 mismatch")
            return {
                "transport": "platform_http_to_windows_spool",
                "bytes": total,
                "sha256": observed_sha,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "chunk_bytes": 1024 * 1024,
                "verified": True,
            }

        transfer = await asyncio.to_thread(download)
        return destination, transfer

    async def _deploy(self, websocket: Any, job: dict[str, Any]) -> dict[str, Any]:
        payload = _record(job.get("payload"))
        serial = _safe_serial(payload.get("device_serial"))
        remote = _safe_remote_path(payload.get("remote_path"))
        artifact_sha = _text(payload.get("artifact_sha256"), 80)
        await self._send_job_progress(
            websocket,
            job,
            stage="artifact_download_started",
            detail_zh="Windows Bridge 正在从 Linux 平台流式下载构建产物。",
        )
        try:
            source, transfer = await self._download_artifact(job, _text(payload.get("download_url"), 2048), artifact_sha)
        except Exception as exc:
            return {
                "status": "failed",
                "stage": "artifact_download",
                "failure_domain": "linux_to_windows_transport",
                "device_serial": serial,
                "error": {
                    "code": "BRIDGE_ARTIFACT_DOWNLOAD_FAILED",
                    "message": str(exc)[:1200],
                },
            }
        await self._send_job_progress(
            websocket,
            job,
            stage="artifact_download_verified",
            detail_zh="构建产物已到达 Windows 临时 spool，SHA-256 校验通过。",
            bytes=transfer["bytes"],
            duration_ms=transfer["duration_ms"],
            artifact_sha256=transfer["sha256"],
        )
        parent = _remote_parent(remote)
        mkdir = await self._run_hdc("-t", serial, "shell", f"mkdir -p {parent}", timeout_seconds=90, job_id=str(job["job_id"]))
        if mkdir["return_code"] != 0:
            return {"status": "failed", "stage": "mkdir", "failure_domain": "device_filesystem", "device_serial": serial, "artifact_transfer": transfer, "hdc": mkdir}
        await self._send_job_progress(
            websocket,
            job,
            stage="hdc_file_send_started",
            detail_zh="Windows Bridge 正在通过本机 hdc.exe 把已校验产物下发到手机。",
            device_serial=serial,
            remote_path=remote,
        )
        sent = await self._run_hdc("-t", serial, "file", "send", str(source), remote, timeout_seconds=240, job_id=str(job["job_id"]))
        if sent["return_code"] != 0:
            return {"status": "failed", "stage": "file_send", "failure_domain": "windows_hdc_to_device_transport", "device_serial": serial, "artifact_transfer": transfer, "hdc": sent}
        await self._send_job_progress(
            websocket,
            job,
            stage="hdc_file_send_completed",
            detail_zh="HDC 文件发送已返回成功，正在核验手机端文件内容。",
            device_serial=serial,
            remote_path=remote,
        )
        executable = await self._run_hdc("-t", serial, "shell", f"chmod 700 {remote}", timeout_seconds=60, job_id=str(job["job_id"]))
        if executable["return_code"] != 0:
            return {"status": "failed", "stage": "chmod", "failure_domain": "device_filesystem", "device_serial": serial, "artifact_transfer": transfer, "hdc": executable}
        checked = await self._run_hdc("-t", serial, "shell", f"(sha256sum {remote} || toybox sha256sum {remote}) 2>/dev/null", timeout_seconds=90, job_id=str(job["job_id"]))
        observed = checked["stdout"].split()[0] if checked["stdout"].split() else ""
        if checked["return_code"] != 0 or observed != artifact_sha:
            return {"status": "failed", "stage": "remote_hash_verification", "failure_domain": "device_integrity", "device_serial": serial, "artifact_transfer": transfer, "expected_sha256": artifact_sha, "observed_sha256": observed or None, "hdc": checked}
        await self._send_job_progress(
            websocket,
            job,
            stage="device_deployment_verified",
            detail_zh="手机端文件已落盘，执行权限与 SHA-256 校验均通过。",
            device_serial=serial,
            remote_path=remote,
        )
        return {
            "status": "succeeded",
            "schema_version": DEPLOYMENT_CONTRACT_VERSION,
            "device_serial": serial,
            "remote_path": remote,
            "artifact_sha256": artifact_sha,
            "remote_sha256": observed,
            "artifact_transfer": transfer,
            "device_transfer": {
                "transport": "windows_hdc_to_device",
                "remote_path": remote,
                "sha256": observed,
                "verified": True,
            },
            "deployment_verified": True,
            "executable": True,
        }

    async def _execute_shell(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = _record(job.get("payload"))
        serial = _safe_serial(payload.get("device_serial"))
        command = _text(payload.get("command"), 16 * 1024)
        if not command:
            raise ValueError("remote shell command missing")
        deadline = self._operation_deadline(job, float(payload.get("timeout_seconds", 240)))
        result = await self._run_hdc("-t", serial, "shell", command, timeout_seconds=max(0.0, deadline - time.monotonic()), job_id=str(job["job_id"]))
        return {
            "status": "timed_out" if result["timed_out"] else "succeeded" if result["return_code"] == 0 else "failed",
            "device_serial": serial,
            "execution": result,
            "workstation_execution_boundary": {"host_shell": False, "hdc_argv": "fixed executable + -t serial + shell + one remote argument"},
        }

    def _trace_paths(self, project_id: str, trace_id: str) -> tuple[Path, Path]:
        if not project_id or not re.fullmatch(r"trace_[a-f0-9]{24}", trace_id):
            raise ValueError("trace identity or project missing")
        project = "project_" + hashlib.sha256(project_id.encode()).hexdigest()[:16]
        directory = self.trace_root / project / trace_id
        for current in (self.trace_root, self.trace_root / project, directory, directory / "metadata.json", directory / "trace.ftrace"):
            if current.is_symlink() or not current.resolve().is_relative_to(self.trace_root):
                raise ValueError("trace storage path is not an owned regular path")
        return directory / "metadata.json", directory / "trace.ftrace"

    def _trace_copy_id(self, project_id: str, trace_id: str) -> str:
        return "copy_" + hashlib.sha256(f"{project_id}\nbridge\n{self.bridge_id}\n{trace_id}".encode()).hexdigest()[:24]

    @staticmethod
    def _trace_sha(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _write_trace_metadata(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
        if temporary.is_symlink():
            raise ValueError("trace metadata temporary path is a symlink")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_trace(self, project_id: str, trace_id: str) -> tuple[dict[str, Any], Path, Path]:
        metadata, raw = self._trace_paths(project_id, trace_id)
        value = json.loads(metadata.read_text(encoding="utf-8"))
        if (value.get("project_id") != project_id or value.get("trace_id") != trace_id
                or value.get("location", {}).get("copy_id") != self._trace_copy_id(project_id, trace_id)):
            raise ValueError("trace metadata identity mismatch")
        value["location"]["local_path"] = str(raw)
        if value["location"].get("status") != "deleted":
            value["location"]["status"] = "available" if raw.is_file() and raw.stat().st_size == value["size_bytes"] else "missing"
        value["location"]["observed_at"] = datetime.now(UTC).isoformat()
        return value, metadata, raw

    def _retain_trace(self, job: dict[str, Any], source: Path) -> dict[str, Any]:
        request = _record(_record(job.get("payload")).get("retain_trace"))
        project_id = _text(job.get("project_id"), 120)
        trace_id, run_id = _text(request.get("trace_id"), 80), _text(request.get("run_id"), 80)
        relative = _text(request.get("relative_path"), 200)
        expected = "trace_" + hashlib.sha256(f"{project_id}\n{run_id}\n{relative}".encode()).hexdigest()[:24]
        if (not re.fullmatch(r"capture_[a-f0-9]{24}", run_id) or not re.fullmatch(r"[0-9]{2,3}_[A-Za-z0-9_-]+/trace\.ftrace", relative)
                or trace_id != expected or request.get("copy_id") != self._trace_copy_id(project_id, trace_id)):
            raise ValueError("retained trace identity mismatch")
        metadata, raw = self._trace_paths(project_id, trace_id)
        before = source.stat()
        digest = self._trace_sha(source)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("collected trace changed during retention")
        if metadata.exists():
            previous, _, _ = self._read_trace(project_id, trace_id)
            if previous["location"]["status"] == "available" and previous["sha256"] == digest:
                return previous
            raise ValueError("trace identity already exists; existing/deleted data is not overwritten")
        raw.parent.mkdir(parents=True, exist_ok=True)
        self._trace_paths(project_id, trace_id)
        value = {"schema_version": "retained_trace/v1", "trace_id": trace_id, "project_id": project_id,
            "run_id": run_id, "plan_id": _text(request.get("plan_id"), 80), "step_id": _text(request.get("step_id"), 80),
            "step_title": _text(request.get("step_title"), 200), "name": "trace.ftrace", "source": "scenario_capture",
            "device_serial": _text(_record(job.get("payload")).get("device_serial"), 160),
            "created_at": datetime.now(UTC).isoformat(), "sha256": digest, "size_bytes": before.st_size,
            "location": {"copy_id": self._trace_copy_id(project_id, trace_id), "kind": "windows_bridge",
                "bridge_id": self.bridge_id, "local_path": str(raw), "storage_host": socket.gethostname(),
                "connection_mode": "bridge", "status": "available", "observed_at": datetime.now(UTC).isoformat(),
                "label": "Windows Bridge 原始文件", "can_download": False, "can_delete": True}}
        if raw.exists():
            # A process can stop after publishing the complete raw file but
            # before its metadata rename. An exact deterministic identity and
            # content hash make that retry idempotent; different bytes remain
            # protected and are never overwritten.
            if raw.is_symlink() or not raw.is_file() or raw.stat().st_size != before.st_size or self._trace_sha(raw) != digest:
                raise ValueError("unregistered trace file exists with different data; file is not overwritten")
            self._write_trace_metadata(metadata, value)
            return value
        try:
            os.replace(source, raw)
        except OSError:
            temporary = raw.with_name(f".{raw.name}.{secrets.token_hex(12)}.partial")
            try:
                if temporary.exists() or temporary.is_symlink():
                    raise ValueError("trace retention staging path already exists")
                shutil.copyfile(source, temporary)
                if self._trace_sha(temporary) != digest:
                    raise ValueError("trace changed while copying into persistent storage")
                temporary.replace(raw)
                source.unlink()
            finally:
                temporary.unlink(missing_ok=True)
        self._write_trace_metadata(metadata, value)
        return value

    async def _trace_library(self, job: dict[str, Any]) -> dict[str, Any]:
        project_id = _text(job.get("project_id"), 120)
        if not project_id:
            raise ValueError("trace library project is required")
        payload = _record(job.get("payload"))
        async with self._trace_lock:
            if job["action"] == "trace.library.list":
                project = self.trace_root / ("project_" + hashlib.sha256(project_id.encode()).hexdigest()[:16])
                if project.is_symlink():
                    raise ValueError("trace project directory is a symlink")
                def inventory():
                    items = []
                    cursor = str(payload.get("cursor") or "0")
                    if not re.fullmatch(r"[0-9]{1,6}", cursor):
                        raise ValueError("trace list cursor invalid")
                    offset = int(cursor)
                    limit = max(1, min(50, int(payload.get("limit", 50))))
                    directories = sorted(project.glob("trace_*"))[:500]
                    for directory in directories[offset:offset + limit]:
                        try:
                            value, _, _ = self._read_trace(project_id, directory.name)
                            items.append(value)
                        except (ValueError, OSError, KeyError, TypeError):
                            continue
                    return {"items": items, "next_cursor": str(offset + limit) if offset + limit < len(directories) else None}
                return {"status": "succeeded", "schema_version": "trace_library/v1", **await asyncio.to_thread(inventory)}
            trace_id = _text(payload.get("trace_id"), 80)
            if trace_id in self._active_trace_ids:
                raise ValueError("trace collection/upload is active; file was not deleted")
            value, metadata, raw = self._read_trace(project_id, trace_id)
            if payload.get("copy_id") != value["location"]["copy_id"]:
                raise ValueError("trace copy identity mismatch")
            def remove():
                if raw.exists():
                    before = raw.lstat()
                    if not stat.S_ISREG(before.st_mode) or before.st_size != value["size_bytes"] or self._trace_sha(raw) != value["sha256"]:
                        raise ValueError("trace file changed; file was not deleted")
                    after = raw.lstat()
                    if (before.st_dev, before.st_ino, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns):
                        raise ValueError("trace file changed before deletion")
                    raw.unlink()
                value["location"].update(status="deleted", deleted_at=datetime.now(UTC).isoformat(), can_delete=False)
                self._write_trace_metadata(metadata, value)
                return value
            return {"status": "succeeded", "trace_id": trace_id, "copy_id": payload["copy_id"], "item": await asyncio.to_thread(remove)}

    async def _collect(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = _record(job.get("payload"))
        serial = _safe_serial(payload.get("device_serial"))
        remote = _safe_remote_path(payload.get("remote_path"))
        destination = self.spool_root / str(job["job_id"]) / "collected.bin"
        transport_destination = destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        deadline = self._operation_deadline(job, float(payload.get("timeout_seconds", 600)))
        result = await self._run_hdc("-t", serial, "file", "recv", remote, str(destination),
                                     timeout_seconds=max(0.0, deadline - time.monotonic()), job_id=str(job["job_id"]))
        if result["return_code"] != 0 or not destination.is_file():
            return {"status": "cancelled" if result.get("cancelled") else "timed_out" if result.get("timed_out") else "failed", "device_serial": serial, "hdc": result}
        if time.monotonic() >= deadline:
            return {"status": "timed_out", "device_serial": serial, "error": "Collection deadline reached after device receive"}
        if destination.stat().st_size > MAX_COLLECT_BYTES:
            destination.unlink(missing_ok=True)
            return {"status": "failed", "device_serial": serial, "error": "collected file exceeds bridge limit"}
        retained = None
        if payload.get("retain_trace"):
            try:
                async with self._trace_lock:
                    retained = await asyncio.to_thread(self._retain_trace, job, destination)
                    self._active_trace_ids.add(retained["trace_id"])
                destination = Path(retained["location"]["local_path"])
            except Exception as exc:
                return {"status": "failed", "device_serial": serial, "error": f"persistent trace retention failed: {exc}"}
        # Upload is intentionally a separate authenticated API call.  The
        # Platform will retain it as an Artifact; a Windows path is never sent.
        url = urljoin(self.platform_http_url + "/", f"v1/device-bridges/jobs/{job['job_id']}/collected")
        upload_stopped = threading.Event()
        upload_task: asyncio.Task[dict[str, Any]] | None = None

        def remaining_budget() -> float:
            if upload_stopped.is_set() or str(job["job_id"]) in self.cancelled_jobs:
                raise RuntimeError("Collection upload cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Collection deadline reached during upload")
            return remaining

        try:
            def upload() -> dict[str, Any]:
                digest = hashlib.sha256()
                size = 0
                with destination.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        remaining_budget()
                        size += len(chunk)
                        if size > MAX_COLLECT_BYTES:
                            raise RuntimeError("collected file exceeds bridge limit")
                        digest.update(chunk)
                    sha256 = digest.hexdigest()
                    source.seek(0)

                    def chunks():
                        sent = 0
                        while block := source.read(1024 * 1024):
                            remaining_budget()
                            sent += len(block)
                            if sent > size:
                                raise RuntimeError("Collected file grew during upload")
                            yield block
                        if sent != size:
                            raise RuntimeError("Collected file changed during upload")

                    request = Request(url, data=chunks(), headers={
                        "Content-Type": "application/octet-stream", "Content-Length": str(size),
                        "X-Bridge-Id": self.bridge_id, "X-Bridge-Secret": self.bridge_secret,
                        "X-Artifact-Sha256": sha256,
                    }, method="POST")
                    with _direct_urlopen(request, timeout=remaining_budget()) as response:
                        acknowledgement = json.loads(response.read(64 * 1024).decode("utf-8"))
                    remaining_budget()
                    if acknowledgement.get("status") != "stored" or acknowledgement.get("sha256") != sha256 or acknowledgement.get("bytes") != size or acknowledgement.get("job_id") != job["job_id"]:
                        raise RuntimeError("Platform did not acknowledge the expected collected file")
                    return {"bytes": size, "sha256": sha256}

            def upload_with_retention_guard() -> dict[str, Any]:
                try:
                    return upload()
                finally:
                    # The network thread may outlive cancellation of its
                    # asyncio caller. Keep the copy protected until it exits.
                    if retained:
                        self._active_trace_ids.discard(retained["trace_id"])

            remaining = remaining_budget()
            upload_task = asyncio.create_task(asyncio.to_thread(upload_with_retention_guard))

            def upload_finished(task: asyncio.Task[dict[str, Any]]) -> None:
                # A timed-out caller leaves the bounded network worker to
                # finish; consume its result while retaining its delete guard.
                if not task.cancelled():
                    task.exception()

            upload_task.add_done_callback(upload_finished)
            done, _ = await asyncio.wait((upload_task,), timeout=remaining)
            if not done:
                raise TimeoutError("Collection upload exceeded its remaining budget")
            transferred = upload_task.result()
        except asyncio.CancelledError:
            upload_stopped.set()
            raise
        except TimeoutError as exc:
            upload_stopped.set()
            return {"status": "timed_out", "device_serial": serial, "error": f"collected file upload deadline reached: {exc}",
                    **({"retained_trace": retained} if retained else {})}
        except Exception as exc:
            return {"status": "failed", "device_serial": serial, "error": f"collected file upload failed: {exc}",
                    **({"retained_trace": retained} if retained else {})}
        finally:
            if retained and upload_task is None:
                self._active_trace_ids.discard(retained["trace_id"])
        # The platform has acknowledged the exact hash and byte count. Replay
        # uses the result spool, not this potentially multi-GiB transport copy.
        try:
            transport_destination.unlink(missing_ok=True)
            try:
                transport_destination.parent.rmdir()
            except OSError:
                pass
            cleanup = "completed"
        except OSError:
            cleanup = "pending"
        return {"status": "succeeded", "device_serial": serial, "remote_path": remote,
                "transfer_spool_cleanup": cleanup, **transferred, **({"retained_trace": retained} if retained else {})}

    async def _execute_action(self, websocket: Any, job: dict[str, Any]) -> dict[str, Any]:
        action = str(job["action"])
        if action == "device.discover":
            return await self._discover(job)
        if action == "device.probe":
            return await self._probe(websocket, job)
        if action == "device.deploy_artifact":
            return await self._deploy(websocket, job)
        if action == "device.execute_shell":
            return await self._execute_shell(job)
        if action == "device.collect_artifact":
            return await self._collect(job)
        if action in {"trace.library.list", "trace.library.delete"}:
            return await self._trace_library(job)
        return {"status": "failed", "error": "unsupported action"}

    async def _execute_job(self, websocket: Any, job: dict[str, Any]) -> None:
        job_id = _text(job.get("job_id"), 80)
        try:
            self._verify_job(job)
        except Exception as exc:
            await websocket.send(json.dumps({
                "type": "job.result",
                "job_id": job_id,
                "request_id": job.get("request_id"),
                "bridge_id": self.bridge_id,
                "started_at_epoch": time.time(),
                "finished_at_epoch": time.time(),
                "result": {"status": "failed", "stage": "job_validation", "failure_domain": "bridge_job_contract", "error": f"device job rejected: {str(exc)[:600]}"},
            }, ensure_ascii=False))
            return
        previous = _record(self.jobs.get(job_id))
        if previous.get("state") in {"succeeded", "failed", "timed_out", "cancelled"} and isinstance(previous.get("result_envelope"), dict):
            # A reconnect may re-offer a job after the Bridge finished but
            # before the backend recorded its result. Re-send the immutable
            # observation instead of repeating the device-side command.
            await websocket.send(json.dumps(previous["result_envelope"], ensure_ascii=False))
            return
        self._persist_job(job_id, {
            "job_id": job_id,
            "request_id": job.get("request_id"),
            "idempotency_key": job.get("idempotency_key"),
            "state": "running",
            "received_at": datetime.now(UTC).isoformat(),
        })
        started = time.time()
        action = str(job["action"])
        deadline = self._operation_deadline(job, MAX_JOB_SECONDS)
        self._job_deadlines[job_id] = deadline
        print(json.dumps({
            "status": "bridge_job_started",
            "job_id": job_id,
            "action": action,
        }, ensure_ascii=False), flush=True)
        try:
            async with asyncio.timeout_at(deadline):
                await websocket.send(json.dumps({"type": "job.accepted", "job_id": job_id, "received_at": datetime.now(UTC).isoformat()}))
                await self._send_job_progress(
                    websocket, job, stage="bridge_job_started",
                    detail_zh="Windows Bridge 已接收任务，正在准备对应的设备操作。",
                )
                output = await self._execute_action(websocket, job)
                if time.monotonic() >= deadline:
                    raise TimeoutError
        except TimeoutError:
            output = {"status": "timed_out", "stage": "bridge_action", "failure_domain": "bridge_job_deadline",
                      "error": {"code": "BRIDGE_JOB_DEADLINE", "message": "Device job deadline reached; queued or subsequent HDC commands were stopped."}}
        except Exception as exc:
            output = {
                "status": "failed",
                "stage": "bridge_action",
                "failure_domain": "windows_bridge_runtime",
                "error": {
                    "code": "BRIDGE_ACTION_FAILED",
                    "message": str(exc)[:1200],
                },
            }
        finally:
            self._job_deadlines.pop(job_id, None)
            self._job_cancel_events.pop(job_id, None)
        if self._runtime_diagnostics:
            output.setdefault("bridge_diagnostics", self._runtime_diagnostics)
        if job_id in self.cancelled_jobs:
            output = {
                "status": "cancelled",
                "detail_zh": "Bridge 已收到取消请求并终止或阻止后续 HDC 操作。",
                "partial_result": output,
            }
        envelope = {
            "type": "job.result",
            "job_id": job_id,
            "request_id": job.get("request_id"),
            "bridge_id": self.bridge_id,
            "started_at_epoch": started,
            "finished_at_epoch": time.time(),
            "result": output,
        }
        self._persist_job(job_id, {
            "job_id": job_id,
            "request_id": job.get("request_id"),
            "idempotency_key": job.get("idempotency_key"),
            "state": _text(output.get("status"), 40) or "failed",
            "finished_at": datetime.now(UTC).isoformat(),
            "result_envelope": envelope,
        })
        print(json.dumps({
            "status": "bridge_job_finished",
            "job_id": job_id,
            "action": action,
            "result_status": _text(output.get("status"), 40) or "failed",
            "duration_ms": round((time.time() - started) * 1000, 3),
        }, ensure_ascii=False), flush=True)
        self.cancelled_jobs.discard(job_id)
        await websocket.send(json.dumps(envelope, ensure_ascii=False))

    async def _run_http_poll_connection(self) -> None:
        if not self.bridge_id or not self.bridge_secret:
            raise RuntimeError("bridge configuration is incomplete")

        async def refresh_diagnostics() -> None:
            force = False
            while True:
                if self._job_tasks:
                    # Device work has priority over a background HDC probe.
                    # Recheck without queuing additional diagnostic tasks.
                    await asyncio.sleep(5)
                    continue
                try:
                    await self._refresh_runtime_diagnostics(force=force)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(json.dumps({
                        "status": "bridge_diagnostics_refresh_failed",
                        "exception_type": type(exc).__name__,
                    }), file=sys.stderr, flush=True)
                force = True
                await asyncio.sleep(DIAGNOSTICS_REFRESH_SECONDS)

        # HDC can take longer than the server's heartbeat grace period. Keep
        # the authenticated HTTP channel alive while its one diagnostic task
        # waits for a slow command, including during initial device discovery.
        connection_id = f"connection_{secrets.token_hex(16)}"
        connection_ready = asyncio.Event()

        async def flush_events(*, replay: bool) -> None:
            # Events carry the same connection identity as polling. Wait for
            # the first successful poll before publishing this session's ACKs.
            await connection_ready.wait()
            while True:
                await self._flush_http_events(connection_id, replay=replay)
                await asyncio.sleep(0.2)

        diagnostics_task = asyncio.create_task(refresh_diagnostics(), name="bridge-http-diagnostics")
        # Historical spool delivery can require hundreds of HTTP requests.
        # Keep it independent of live ACK/progress/result delivery so an
        # online Bridge's fresh target list cannot wait behind that history.
        events_task = asyncio.create_task(flush_events(replay=False), name="bridge-http-events")
        replay_task = asyncio.create_task(flush_events(replay=True), name="bridge-http-replay")
        poll_task = asyncio.create_task(
            self._poll_http_messages(connection_id, connection_ready), name="bridge-http-poll"
        )
        try:
            # A failed event POST is a failed connection too. Observe it here
            # so the ordinary reconnect path retries unacknowledged envelopes
            # instead of leaving a detached task or silently losing results.
            finished, _ = await asyncio.wait((poll_task, events_task, replay_task), return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                task.result()
        finally:
            for task in (diagnostics_task, events_task, replay_task, poll_task):
                task.cancel()
            await asyncio.gather(diagnostics_task, events_task, replay_task, poll_task, return_exceptions=True)

    async def _poll_http_messages(self, connection_id: str, connection_ready: asyncio.Event) -> None:
        transport = _QueuedHttpEventTransport(self)
        poll_url = urljoin(self.platform_http_url + "/", "v1/device-bridges/poll")
        reconcile_jobs: list[dict[str, Any]] | None = self._known_jobs()
        self._queue_completed_results()
        next_console_heartbeat = time.monotonic() + CONSOLE_HEARTBEAT_SECONDS
        last_console_device_count = int(_record(_record(self._runtime_diagnostics).get("hdc")).get("target_count", 0))

        while True:
            submitted_reconcile = reconcile_jobs is not None
            response = await asyncio.to_thread(
                _http_json,
                poll_url,
                {
                    "connection_id": connection_id,
                    "capabilities": self.capabilities(),
                    "reconcile_jobs": reconcile_jobs,
                    "max_messages": 32,
                },
                timeout=30,
                extra_headers=self._bridge_headers(),
            )
            reconcile_jobs = None
            if (
                response.get("schema_version") != "device_bridge_http_poll/v1"
                or response.get("protocol_version") != HTTP_POLL_PROTOCOL_VERSION
                or response.get("bridge_id") != self.bridge_id
            ):
                raise RuntimeError("platform returned an invalid HTTP poll response")
            connection_ready.set()
            if response.get("new_connection") is True and not submitted_reconcile:
                # A fast backend restart may occur between two successful
                # HTTP requests.  The response tells the Bridge that server
                # memory was rebuilt, so the next poll resubmits disk-backed
                # job state even though no socket-level error was observed.
                reconcile_jobs = self._known_jobs()
            messages = response.get("messages") if isinstance(response.get("messages"), list) else []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                kind = message.get("type")
                if kind == "job.offer" and isinstance(message.get("job"), dict):
                    self._schedule_job(transport, message["job"])
                elif kind == "job.cancel":
                    job_id = _text(message.get("job_id"), 80)
                    await self._cancel_device_job(job_id)
                    await transport.send(json.dumps({"type": "job.cancelled", "job_id": job_id}))
            self._queue_completed_results()
            now = time.monotonic()
            diagnostics = self._runtime_diagnostics
            device_count = int(_record(diagnostics.get("hdc")).get("target_count", 0))
            if device_count != last_console_device_count or now >= next_console_heartbeat:
                print(json.dumps({
                    "status": "bridge_alive",
                    "transport": "http_poll",
                    "diagnostics_status": diagnostics.get("status"),
                    "device_count": device_count,
                    "active_job_count": len(self._job_tasks),
                }, ensure_ascii=False), flush=True)
                last_console_device_count = device_count
                next_console_heartbeat = now + CONSOLE_HEARTBEAT_SECONDS
            poll_after_ms = response.get("poll_after_ms")
            delay_seconds = max(0.2, min(float(poll_after_ms) / 1000.0, 5.0)) if isinstance(poll_after_ms, (int, float)) else 0.75
            await asyncio.sleep(delay_seconds)

    async def _cancel_device_job(self, job_id: str) -> None:
        """Wake queued jobs and reap an owned HDC child before acknowledging."""

        self.cancelled_jobs.add(job_id)
        self._job_cancel_events.setdefault(job_id, asyncio.Event()).set()
        finished = self._hdc_finished.get(job_id)
        process = self.processes.get(job_id)
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if finished is not None:
            # This also covers cancellation while subprocess creation is
            # awaiting a handle. No terminal ACK precedes owned-child cleanup.
            await asyncio.wait_for(finished.wait(), timeout=7)

    async def _run_websocket_connection(self) -> None:
        if not self.bridge_id or not self.bridge_secret or not self.platform_ws_url:
            raise RuntimeError("bridge configuration is incomplete")
        await self._refresh_runtime_diagnostics()
        # Keep the secret out of the WebSocket URL.  URLs are routinely copied
        # into proxy/access logs while the first WSS frame is protected by TLS
        # and handled only by the Platform's authenticated handshake.
        async with websockets.connect(
            self.platform_ws_url,
            max_size=2 * 1024 * 1024,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
            **_direct_websocket_options(),
        ) as websocket:
            await websocket.send(json.dumps({
                "type": "bridge.hello",
                "protocol_version": WEBSOCKET_PROTOCOL_VERSION,
                "bridge_id": self.bridge_id,
                "bridge_secret": self.bridge_secret,
                "capabilities": self.capabilities(),
            }))
            await websocket.send(json.dumps({"type": "job.reconcile", "jobs": self._known_jobs()}))
            # Re-publish completed spool entries after reconnect.  The backend
            # deduplicates by job_id and records the same immutable observation;
            # this is what closes the gap between a finished HDC process and a
            # lost WebSocket result frame.
            for item in self.jobs.values():
                envelope = item.get("result_envelope")
                if isinstance(envelope, dict):
                    await websocket.send(json.dumps(envelope, ensure_ascii=False))

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(10)
                    await websocket.send(json.dumps({"type": "bridge.heartbeat", "capabilities": self.capabilities()}))

            async def refresh_diagnostics() -> None:
                while True:
                    await asyncio.sleep(DIAGNOSTICS_REFRESH_SECONDS)
                    # HDC probing has its own task so a slow local command can
                    # never starve the transport heartbeat and mark an otherwise
                    # healthy Bridge offline.
                    await self._refresh_runtime_diagnostics(force=True)

            heartbeat_task = asyncio.create_task(heartbeat())
            diagnostics_task = asyncio.create_task(refresh_diagnostics())
            try:
                async for raw in websocket:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        continue
                    kind = message.get("type")
                    if kind == "job.offer" and isinstance(message.get("job"), dict):
                        self._schedule_job(websocket, message["job"])
                    elif kind == "job.reconcile":
                        # The Platform may include an informational list before
                        # sending concrete job.offer messages.  Job execution
                        # remains driven by a signed offer only.
                        continue
                    elif kind == "job.cancel":
                        job_id = _text(message.get("job_id"), 80)
                        await self._cancel_device_job(job_id)
                        await websocket.send(json.dumps({"type": "job.cancelled", "job_id": job_id}))
            finally:
                heartbeat_task.cancel()
                diagnostics_task.cancel()
                await asyncio.gather(heartbeat_task, diagnostics_task, return_exceptions=True)

    async def run(self) -> None:
        """Keep an outbound Bridge channel alive across transient transport loss.

        Completed device jobs are first persisted in the local spool, then
        replayed through ``job.reconcile`` after the next connection.  The loop
        deliberately never replays an in-flight command on its own: the
        Platform's idempotency and reconciliation state decide whether an offer
        is safe to send again.
        """

        diagnostics = (
            self._runtime_diagnostics
            if self.transport == "http_poll"
            else await self._refresh_runtime_diagnostics(force=True)
        )
        hdc = _record(diagnostics.get("hdc"))
        print(json.dumps({
            "status": "bridge_running",
            "bridge_id": self.bridge_id,
            "implementation_version": BRIDGE_IMPLEMENTATION_VERSION,
            "transport": self.transport,
            "protocol_version": HTTP_POLL_PROTOCOL_VERSION if self.transport == "http_poll" else WEBSOCKET_PROTOCOL_VERSION,
            "diagnostics_status": diagnostics.get("status"),
            "device_count": hdc.get("target_count", 0),
            "hdc": str(self.hdc_path),
            "discovery": f"automatic/{DIAGNOSTICS_REFRESH_SECONDS}s",
        }, ensure_ascii=False))
        delay_seconds = 1
        while True:
            try:
                if self.transport == "http_poll":
                    await self._run_http_poll_connection()
                    print("Device Bridge HTTP poll loop ended; reconnecting.", file=sys.stderr)
                else:
                    await self._run_websocket_connection()
                    print("Device Bridge WebSocket closed; reconnecting.", file=sys.stderr)
                delay_seconds = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                label = "HTTP transport temporarily unavailable" if self.transport == "http_poll" else "WebSocket compatibility transport interrupted"
                print(f"Device Bridge {label}: {str(exc)[:500]}", file=sys.stderr)
            await asyncio.sleep(delay_seconds)
            delay_seconds = min(30, max(1, delay_seconds * 2))


def _default_config_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", "")) if os.name == "nt" else Path.home() / ".config"
    return base / "HeterogeneousDeviceBridge" / "bridge.json"


def _save_config(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _acquire_run_lock(config_path: Path) -> Any:
    """Prevent two Bridge daemons from sharing one identity and job spool."""

    lock_path = config_path.parent / "bridge.run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nstarted_at={datetime.now(UTC).isoformat()}\n".encode("ascii"))
        handle.flush()
        return handle
    except (OSError, BlockingIOError) as exc:
        handle.close()
        raise RuntimeError(
            f"another Device Bridge process already owns {lock_path}; stop the old process before starting a new one"
        ) from exc


def _release_run_lock(handle: Any) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("bridge config root must be an object")
    return value


def pair(args: argparse.Namespace) -> None:
    try:
        platform_url = _validated_platform_url(args.platform_url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    hdc = Path(args.hdc).expanduser().resolve()
    if not hdc.is_file():
        raise SystemExit(f"hdc executable not found: {hdc}")
    config_path = Path(args.config).expanduser().resolve() if args.config else _default_config_path()
    if config_path.is_file() and not args.pairing_code:
        try:
            existing = _load_config(config_path)
        except (OSError, json.JSONDecodeError, RuntimeError):
            existing = {}
        if (
            _text(existing.get("platform_http_url"), 512).rstrip("/") == platform_url
            and _text(existing.get("bridge_id"), 80)
            and _text(existing.get("bridge_secret"), 200)
        ):
            existing["hdc_path"] = str(hdc)
            existing["spool_root"] = str(config_path.parent / "spool")
            existing["transport"] = "http_poll"
            _save_config(config_path, existing)
            print(json.dumps({
                "status": "configured",
                "bridge_id": existing["bridge_id"],
                "registration_mode": existing.get("registration_mode", "existing_identity"),
                "config": str(config_path),
            }, ensure_ascii=False))
            return
    registration: dict[str, Any] = {
        "display_name": args.name or "Windows HDC Device Bridge",
        "capabilities": {"protocol_version": PROTOCOL_VERSION, "platform": sys.platform, "actions": list(BRIDGE_ACTIONS)},
    }
    if args.pairing_code:
        registration["pairing_code"] = args.pairing_code
    response = _http_json(
        f"{platform_url}/v1/device-bridges/register",
        registration,
        idempotency_key=(
            f"bridge-register-{hashlib.sha256(args.pairing_code.encode('utf-8')).hexdigest()[:48]}"
            if args.pairing_code
            else f"bridge-register-{hashlib.sha256(f'{platform_url}|{args.name}|{hdc}'.encode('utf-8')).hexdigest()[:48]}"
        ),
    )
    ws_scheme = "wss" if platform_url.startswith("https://") else "ws"
    ws_url = ws_scheme + "://" + platform_url.split("://", 1)[1] + "/v1/device-bridges/ws"
    config = {
        "schema_version": "hdc_device_bridge_config/v1",
        "platform_http_url": platform_url,
        "platform_ws_url": ws_url,
        "bridge_id": response["bridge_id"],
        "bridge_secret": response["bridge_secret"],
        "registration_mode": response.get("registration_mode", "pairing_code" if args.pairing_code else "trusted_single_user"),
        "transport": "http_poll",
        "hdc_path": str(hdc),
        "spool_root": str(config_path.parent / "spool"),
    }
    _save_config(config_path, config)
    print(json.dumps({
        "status": "configured",
        "bridge_id": response["bridge_id"],
        "registration_mode": config["registration_mode"],
        "config": str(config_path),
    }, ensure_ascii=False))


def _platform_health(platform_url: str) -> dict[str, Any]:
    url = urljoin(platform_url + "/", "v1/health")
    request = Request(url, headers={"User-Agent": "Heterogeneous-HDC-Device-Bridge/1.0"})
    started = time.monotonic()
    try:
        with _direct_urlopen(request, timeout=20) as response:
            body = response.read(64 * 1024)
            return {
                "reachable": 200 <= int(response.status) < 300,
                "status_code": int(response.status),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "response_preview": _bounded(body, 1200),
            }
    except Exception as exc:
        return {
            "reachable": False,
            "status_code": None,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "error": f"{type(exc).__name__}: {str(exc)[:1200]}",
        }


async def _platform_websocket_health(bridge: HdcDeviceBridge) -> dict[str, Any]:
    started = time.monotonic()
    try:
        async with websockets.connect(
            bridge.platform_ws_url,
            compression=None,
            open_timeout=20,
            close_timeout=5,
            ping_interval=None,
            **_direct_websocket_options(),
        ) as websocket:
            await websocket.send(json.dumps({
                "type": "bridge.hello",
                "protocol_version": WEBSOCKET_PROTOCOL_VERSION,
                "bridge_id": bridge.bridge_id,
                "bridge_secret": bridge.bridge_secret,
                "capabilities": bridge.capabilities(),
            }))
            raw = await asyncio.wait_for(websocket.recv(), timeout=20)
            welcome = json.loads(raw)
            if (
                not isinstance(welcome, dict)
                or welcome.get("type") != "bridge.welcome"
                or welcome.get("bridge_id") != bridge.bridge_id
                or welcome.get("protocol_version") != WEBSOCKET_PROTOCOL_VERSION
            ):
                raise RuntimeError("platform returned an invalid Bridge welcome frame")
            return {
                "reachable": True,
                "protocol_verified": True,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
    except Exception as exc:
        return {
            "reachable": False,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "error": f"{type(exc).__name__}: {str(exc)[:1200]}",
        }


async def _platform_http_poll_health(bridge: HdcDeviceBridge) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = await asyncio.to_thread(
            _http_json,
            urljoin(bridge.platform_http_url + "/", "v1/device-bridges/poll"),
            {
                "connection_id": f"connection_{secrets.token_hex(16)}",
                "capabilities": bridge.capabilities(),
                "reconcile_jobs": None,
                "max_messages": 1,
                "probe_only": True,
            },
            timeout=20,
            extra_headers=bridge._bridge_headers(),
        )
        if (
            response.get("probe_verified") is not True
            or response.get("bridge_id") != bridge.bridge_id
            or response.get("protocol_version") != HTTP_POLL_PROTOCOL_VERSION
        ):
            raise RuntimeError("platform returned an invalid HTTP poll probe response")
        return {
            "reachable": True,
            "protocol_verified": True,
            "protocol_version": HTTP_POLL_PROTOCOL_VERSION,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }
    except Exception as exc:
        return {
            "reachable": False,
            "protocol_verified": False,
            "protocol_version": HTTP_POLL_PROTOCOL_VERSION,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "error": f"{type(exc).__name__}: {str(exc)[:1200]}",
        }


async def _doctor_report(
    bridge: HdcDeviceBridge,
    *,
    transfer_check: bool,
    requested_serial: str,
) -> dict[str, Any]:
    diagnostics = await bridge._refresh_runtime_diagnostics(force=True)
    platform = await asyncio.to_thread(_platform_health, bridge.platform_http_url)
    platform_http_poll = await _platform_http_poll_health(bridge)
    platform_websocket = (
        await _platform_websocket_health(bridge)
        if bridge.transport == "websocket"
        else {
            "reachable": None,
            "protocol_verified": None,
            "status": "compatibility_not_required",
            "detail_zh": "当前 Bridge 默认使用 HTTP 轮询；WebSocket 仅保留兼容，不参与健康门禁。",
        }
    )
    report: dict[str, Any] = {
        "schema_version": "device_bridge_doctor/v1",
        "checked_at": datetime.now(UTC).isoformat(),
        "config": {
            "config_path": str(bridge.config_path),
            "platform_http_url": bridge.platform_http_url,
            "platform_ws_url": bridge.platform_ws_url,
            "transport": bridge.transport,
            "hdc_path": str(bridge.hdc_path),
            "spool_root": str(bridge.spool_root),
            "bridge_id": bridge.bridge_id,
        },
        "platform": platform,
        "platform_http_poll": platform_http_poll,
        "platform_websocket": platform_websocket,
        "runtime_diagnostics": diagnostics,
    }
    transfer: dict[str, Any] | None = None
    if transfer_check:
        hdc = _record(diagnostics.get("hdc"))
        targets = _record(hdc.get("targets"))
        serials = bridge._target_serials(targets)
        serial = _safe_serial(requested_serial) if requested_serial else (serials[0] if serials else "")
        if diagnostics.get("status") == "failed":
            transfer = {
                "status": "failed",
                "failure_domain": _text(_record(diagnostics.get("issues")[0] if diagnostics.get("issues") else {}).get("failure_domain"), 120) or "windows_bridge_configuration",
                "error": "Bridge 基础诊断失败，未执行设备写入检查。",
            }
        elif not serial:
            transfer = {
                "status": "failed",
                "failure_domain": "windows_hdc_device_connection",
                "error": "没有可用于文件传输闭环的 HDC target。",
            }
        else:
            payload = os.urandom(96)
            digest = hashlib.sha256(payload).hexdigest()
            local_path = bridge.spool_root / f"bridge-doctor-{digest[:16]}.bin"
            remote_path = f"/data/local/tmp/hetero_bridge_doctor_{digest[:16]}.bin"
            mkdir: dict[str, Any] = {}
            sent: dict[str, Any] = {}
            checked: dict[str, Any] = {}
            cleanup: dict[str, Any] = {}
            try:
                try:
                    local_path.write_bytes(payload)
                    mkdir = await bridge._run_hdc("-t", serial, "shell", "mkdir -p /data/local/tmp", timeout_seconds=30)
                    if mkdir.get("return_code") == 0:
                        sent = await bridge._run_hdc("-t", serial, "file", "send", str(local_path), remote_path, timeout_seconds=90)
                    if sent.get("return_code") == 0:
                        checked = await bridge._run_hdc(
                            "-t",
                            serial,
                            "shell",
                            f"(sha256sum {remote_path} || toybox sha256sum {remote_path}) 2>/dev/null",
                            timeout_seconds=30,
                    )
                    observed = checked.get("stdout", "").split()[0] if checked.get("stdout", "").split() else ""
                    verified = bool(checked.get("return_code") == 0 and observed == digest)
                    if verified:
                        failure_domain = None
                    elif mkdir.get("launch_error") or sent.get("launch_error"):
                        failure_domain = "windows_hdc_process"
                    elif mkdir.get("return_code") != 0:
                        failure_domain = "device_filesystem"
                    elif sent.get("return_code") != 0:
                        failure_domain = "windows_hdc_to_device_transport"
                    else:
                        failure_domain = "device_integrity"
                    transfer = {
                        "status": "succeeded" if verified else "failed",
                        "failure_domain": failure_domain,
                        "device_serial": serial,
                        "remote_path": remote_path,
                        "expected_sha256": digest,
                        "observed_sha256": observed or None,
                        "verified": verified,
                        "mkdir": mkdir,
                        "file_send": sent,
                        "remote_hash": checked,
                    }
                except OSError as exc:
                    transfer = {
                        "status": "failed",
                        "failure_domain": "windows_spool",
                        "device_serial": serial,
                        "error": f"{type(exc).__name__}: {str(exc)[:1200]}",
                    }
            finally:
                try:
                    cleanup = await bridge._run_hdc("-t", serial, "shell", f"rm -f {remote_path}", timeout_seconds=30)
                except Exception as exc:
                    cleanup = {"return_code": None, "stderr": f"cleanup failed: {str(exc)[:1200]}"}
                local_path.unlink(missing_ok=True)
                if transfer is not None:
                    transfer["cleanup"] = cleanup
        report["transfer_check"] = transfer

    failed = (
        diagnostics.get("status") == "failed"
        or platform.get("reachable") is not True
        or platform_http_poll.get("reachable") is not True
        or (bridge.transport == "websocket" and platform_websocket.get("reachable") is not True)
        or (transfer_check and _record(transfer).get("status") != "succeeded")
    )
    attention = diagnostics.get("status") == "attention"
    report["status"] = "failed" if failed else "attention" if attention else "ready"
    report["summary_zh"] = (
        "Windows Device Bridge 诊断失败；请按 failure_domain 和原始命令结果定位。"
        if failed
        else "Bridge 配置和传输闭环可用，但当前存在需要关注的设备连接状态。"
        if attention
        else "Bridge 配置、平台连接、HDC 和所选诊断闭环均可用。"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Heterogeneous Enablement HDC Device Bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    pair_parser = subparsers.add_parser("pair", help="configure and automatically register this Bridge")
    pair_parser.add_argument("--platform-url", required=True)
    pair_parser.add_argument("--pairing-code", default="", help="legacy optional pairing code")
    pair_parser.add_argument("--hdc", required=True)
    pair_parser.add_argument("--name", default="")
    pair_parser.add_argument("--config", default="")
    run_parser = subparsers.add_parser("run", help="connect the configured Bridge over resilient outbound HTTP polling")
    run_parser.add_argument("--config", default=str(_default_config_path()))
    doctor_parser = subparsers.add_parser("doctor", help="diagnose Platform, HDC, spool and optional device transfer")
    doctor_parser.add_argument("--config", default=str(_default_config_path()))
    doctor_parser.add_argument("--transfer-check", action="store_true")
    doctor_parser.add_argument("--device-serial", default="")
    args = parser.parse_args()
    if args.command == "pair":
        pair(args)
        return
    config_path = Path(args.config).expanduser().resolve()
    config = _load_config(config_path)
    bridge = HdcDeviceBridge(config, config_path)
    if args.command == "doctor":
        report = asyncio.run(_doctor_report(
            bridge,
            transfer_check=bool(args.transfer_check),
            requested_serial=_text(args.device_serial, 160),
        ))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(1 if report.get("status") == "failed" else 0)
    try:
        run_lock = _acquire_run_lock(config_path)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        try:
            asyncio.run(bridge.run())
        except KeyboardInterrupt:
            print(json.dumps({"status": "bridge_stopped", "bridge_id": bridge.bridge_id}, ensure_ascii=False))
    finally:
        _release_run_lock(run_lock)


if __name__ == "__main__":
    main()
