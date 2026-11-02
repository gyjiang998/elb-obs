"""Exercise the real Bridge HTTP client against an isolated local gateway.

Only HDC diagnostics/actions are replaced. HTTP serialization, authentication,
polling, signed envelopes, result replay, registry persistence and heartbeats
run through their production implementations without a phone or platform state.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from backend.core import ArtifactStore, PlatformError
from backend.device_gateway import DeviceGateway, HTTP_POLL_PROTOCOL_VERSION
from backend.scenario_capture import ScenarioCaptureManager


BRIDGE_PATH = Path(__file__).resolve().parents[2] / "device-bridge" / "hdc_device_bridge.py"
SPEC = importlib.util.spec_from_file_location("device_bridge_http_liveness", BRIDGE_PATH)
assert SPEC is not None and SPEC.loader is not None
BRIDGE_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE_MODULE)


class _GatewayHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, gateway: DeviceGateway) -> None:
        super().__init__(("127.0.0.1", 0), _GatewayHandler)
        self.gateway = gateway
        self.requests: list[dict[str, Any]] = []
        self.requests_lock = threading.Lock()
        self.slow_events: dict[str, threading.Event] = {}
        self.release_events = threading.Event()
        self.blocked_event_job_ids: set[str] = set()
        self.blocked_event_started = threading.Event()
        self.delayed_event_job_ids: set[str] = set()
        self.event_delay_seconds = 0.0
        self.delayed_event_started = threading.Event()

    def poll_records(self, bridge_id: str) -> list[dict[str, Any]]:
        with self.requests_lock:
            return [item for item in self.requests if item["bridge_id"] == bridge_id and item["path"].endswith("/poll")]


class _GatewayHandler(BaseHTTPRequestHandler):
    server: _GatewayHttpServer

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        started = time.monotonic()
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        bridge_id = self.headers.get("X-Bridge-Id", "")
        bridge_secret = self.headers.get("X-Bridge-Secret", "")
        gateway = self.server.gateway
        code = 200
        response: dict[str, Any]
        try:
            gateway.authenticate_bridge(bridge_id, bridge_secret)
            if self.path == "/v1/device-bridges/poll":
                response = gateway.poll_bridge_messages(
                    bridge_id,
                    bridge_secret,
                    connection_id=payload["connection_id"],
                    capabilities=payload["capabilities"],
                    reconcile_jobs=payload["reconcile_jobs"],
                    max_messages=payload["max_messages"],
                )
                response = {"schema_version": "device_bridge_http_poll/v1", **response, "poll_after_ms": 200}
            elif self.path == "/v1/device-bridges/events":
                message = payload["message"]
                kind = message["type"]
                if kind == "job.result":
                    gateway.report_bridge_result(bridge_id, message)
                elif kind == "job.accepted":
                    gateway.mark_bridge_job_accepted(bridge_id, message["job_id"])
                elif kind == "job.progress":
                    gateway.report_bridge_progress(bridge_id, message)
                elif kind != "bridge.heartbeat":
                    raise AssertionError(f"Unexpected integration event: {kind}")
                gateway.heartbeat(bridge_id)
                response = {
                    "schema_version": "device_bridge_http_event_ack/v1",
                    "protocol_version": HTTP_POLL_PROTOCOL_VERSION,
                    "bridge_id": bridge_id,
                    "acknowledged": True,
                    "event_type": kind,
                    "event_disposition": "accepted",
                }
                if bridge_id in self.server.slow_events:
                    self.server.slow_events[bridge_id].set()
                    self.server.release_events.wait(timeout=5)
                if message.get("job_id") in self.server.blocked_event_job_ids:
                    self.server.blocked_event_started.set()
                    self.server.release_events.wait(timeout=10)
                if message.get("job_id") in self.server.delayed_event_job_ids:
                    self.server.delayed_event_started.set()
                    time.sleep(self.server.event_delay_seconds)
            else:
                raise AssertionError(f"Unexpected integration path: {self.path}")
        except PlatformError as exc:
            code = exc.status_code
            response = {"error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:
            code = 500
            response = {"error": {"code": type(exc).__name__, "message": str(exc)}}
        with self.server.requests_lock:
            self.server.requests.append({
                "bridge_id": bridge_id,
                "path": self.path,
                "payload": payload,
                "response": response,
                "status": code,
                "elapsed": time.monotonic() - started,
            })
        body = json.dumps(response).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class DeviceBridgeHttpLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.gateway = DeviceGateway(workspace_root=self.root, state_root=self.root / "state")
        self.server = _GatewayHttpServer(self.gateway)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.tasks: list[asyncio.Task[None]] = []
        self.bridges: list[Any] = []

    async def asyncTearDown(self) -> None:
        self.server.release_events.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for bridge in self.bridges:
            for task in bridge._job_tasks.values():
                task.cancel()
            await asyncio.gather(*bridge._job_tasks.values(), return_exceptions=True)
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temporary.cleanup()

    def make_bridge(self, name: str) -> Any:
        registration = self.gateway.register_trusted_bridge(
            project_id="local-default", display_name=name, capabilities={},
        )
        bridge = BRIDGE_MODULE.HdcDeviceBridge({
            "bridge_id": registration["bridge_id"],
            "bridge_secret": registration["bridge_secret"],
            "platform_http_url": f"http://127.0.0.1:{self.server.server_port}",
            "hdc_path": str(self.root / "unused-hdc"),
            "spool_root": str(self.root / name / "spool"),
        }, self.root / name / "bridge.json")
        self.bridges.append(bridge)
        return bridge

    def start_bridge(self, bridge: Any) -> asyncio.Task[None]:
        task = asyncio.create_task(bridge._run_http_poll_connection())
        self.tasks.append(task)
        return task

    async def wait_until(self, predicate: Any, *, timeout: float = 4) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                for task in self.tasks:
                    if task.done() and not task.cancelled():
                        task.result()
                await asyncio.sleep(0.02)

    def online_bridges(self) -> dict[str, dict[str, Any]]:
        return {item["bridge_id"]: item for item in self.gateway.list_bridges(project_id="local-default")}

    def seed_finished_history(self, bridge: Any, count: int) -> set[str]:
        """Keep immutable completed jobs on both sides, but no delivery ACK."""
        registry = self.gateway._read_registry()
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        job_ids = set()
        for index in range(count):
            job_id = f"devjob_{index:024x}"
            job = {
                "schema_version": "device_job/v1", "job_id": job_id,
                "request_id": f"req_{index:024x}", "project_id": "local-default",
                "bridge_id": bridge.bridge_id, "idempotency_key": f"backlog-{index}",
                "action": "device.execute_shell", "deadline": deadline,
                "payload": {"device_serial": "phone-live", "command": "echo must-not-run"},
            }
            envelope = {"type": "job.result", "job_id": job_id, "request_id": job["request_id"],
                "bridge_id": bridge.bridge_id,
                "result": {"status": "succeeded", "execution": {"return_code": 0}}}
            registry["jobs"][job_id] = {"job": job, "state": "succeeded", "result": envelope}
            bridge._persist_job(job_id, {**job, "state": "succeeded", "result_envelope": envelope})
            job_ids.add(job_id)
        self.gateway._write_registry(registry)
        return job_ids

    def install_hdc_phone(self, bridge: Any) -> list[tuple[str | None, tuple[str, ...]]]:
        """Use production diagnostics/discovery parsers; only the executable is fake."""
        bridge.hdc_path.touch()
        calls: list[tuple[str | None, tuple[str, ...]]] = []

        async def hdc(*args: str, timeout_seconds: float, job_id: str | None = None,
                      **_kwargs: object) -> dict[str, Any]:
            calls.append((job_id, args))
            self.assertGreater(timeout_seconds, 0)
            self.assertIn(args, [("-v",), ("list", "targets")], "Completed history must never execute again")
            return {"return_code": 0, "timed_out": False, "cancelled": False,
                "stdout": "phone-live\n" if args == ("list", "targets") else "HDC 3.1.0\n",
                "stderr": "", "duration_ms": 1.0}

        bridge._run_hdc = hdc
        return calls

    async def assert_fresh_discovery(self, bridge: Any, hdc_calls: list[Any]) -> str:
        """Dispatch through the same manager as the capture API, without fake results."""
        manager = ScenarioCaptureManager(self.root / "state", ArtifactStore(self.root), self.gateway)
        started = time.monotonic()
        result = await asyncio.to_thread(manager.discover_devices, bridge.bridge_id, timeout_seconds=3)
        self.assertEqual(result["status"], "ready", result)
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(result["discovery_complete"])
        self.assertEqual([item["device_serial"] for item in result["devices"]], ["phone-live"])
        self.assertEqual(result["selected_device"]["bridge_id"], bridge.bridge_id)
        self.assertEqual(result["discovery_profile"], "targets_only")
        job_id = result["device_job_id"]
        self.assertEqual([args for called_job, args in hdc_calls if called_job == job_id], [("list", "targets")])
        await self.wait_until(lambda: job_id in bridge._reported_result_job_ids)
        with self.server.requests_lock:
            live = [item for item in self.server.requests if item["path"].endswith("/events")
                and item["payload"]["message"].get("job_id") == job_id]
            offered = [message["job"] for request in self.server.requests if request["path"].endswith("/poll")
                for message in request["response"].get("messages", [])
                if message.get("type") == "job.offer" and message["job"]["job_id"] == job_id]
        self.assertEqual([item["payload"]["message"]["type"] for item in live],
            ["job.accepted", "job.progress", "job.result"])
        self.assertTrue(all(item["status"] == 200 and item["response"]["acknowledged"] for item in live))
        self.assertEqual(len(offered), 1)
        self.assertEqual(offered[0]["payload"], {"targets_only": True})
        self.assertTrue(offered[0]["signature"], "Production job validation must receive a signed offer")
        return job_id

    async def test_blocked_history_ack_does_not_hide_diagnosed_phone_from_capture_discovery(self) -> None:
        bridge = self.make_bridge("blocked-history-ack")
        calls = self.install_hdc_phone(bridge)
        history = self.seed_finished_history(bridge, 1)
        self.server.blocked_event_job_ids = history
        self.start_bridge(bridge)
        await self.wait_until(self.server.blocked_event_started.is_set)
        await self.wait_until(lambda: self.online_bridges()[bridge.bridge_id]
            .get("capabilities", {}).get("runtime_diagnostics", {}).get("hdc", {}).get("target_count") == 1)
        listed = self.online_bridges()[bridge.bridge_id]
        self.assertEqual(listed["connection_state"], "connected")
        await self.assert_fresh_discovery(bridge, calls)
        self.assertFalse(self.server.release_events.is_set(), "History ACK must remain blocked through fresh discovery")
        self.assertTrue(history.isdisjoint(bridge._reported_result_job_ids))
        self.assertTrue(all(job_id not in history for job_id, _args in calls))

    async def test_slow_history_larger_than_flush_batch_preserves_fresh_discovery_deadline(self) -> None:
        bridge = self.make_bridge("slow-history-backlog")
        calls = self.install_hdc_phone(bridge)
        history = self.seed_finished_history(bridge, 80)
        self.server.delayed_event_job_ids = history
        self.server.event_delay_seconds = 0.08
        self.start_bridge(bridge)
        await self.wait_until(self.server.delayed_event_started.is_set)
        await self.wait_until(lambda: self.online_bridges()[bridge.bridge_id]
            .get("capabilities", {}).get("runtime_diagnostics", {}).get("hdc", {}).get("target_count") == 1)
        job_id = await self.assert_fresh_discovery(bridge, calls)
        self.assertLess(len(history & bridge._reported_result_job_ids), len(history),
            "Discovery must finish while historical delivery is still draining")
        self.assertEqual([called_job for called_job, _args in calls if called_job], [job_id])
        await self.wait_until(lambda: history.issubset(bridge._reported_result_job_ids), timeout=20)
        with self.server.requests_lock:
            history_posts = [item for item in self.server.requests if item["path"].endswith("/events")
                and item["payload"]["message"].get("job_id") in history]
        self.assertEqual(len(history_posts), len(history), "History must be replayed exactly once")
        self.assertEqual({item["payload"]["message"]["job_id"] for item in history_posts}, history)
        self.assertTrue(all(item["payload"]["message"]["type"] == "job.result" for item in history_posts))
        self.assertEqual([called_job for called_job, _args in calls if called_job], [job_id])

    async def test_hanging_hdc_diagnostics_do_not_block_initial_poll_or_heartbeat(self) -> None:
        bridge = self.make_bridge("hanging-hdc")
        diagnostics_started = asyncio.Event()
        diagnostics_released = asyncio.Event()
        diagnostics_cancelled = asyncio.Event()

        async def slow_diagnostics(**_kwargs: object) -> dict[str, Any]:
            diagnostics_started.set()
            try:
                await diagnostics_released.wait()
                return {}
            finally:
                diagnostics_cancelled.set()

        bridge._refresh_runtime_diagnostics = slow_diagnostics
        task = self.start_bridge(bridge)
        await self.wait_until(diagnostics_started.is_set)
        await self.wait_until(lambda: len(self.server.poll_records(bridge.bridge_id)) >= 5)
        self.assertFalse(diagnostics_released.is_set())
        self.assertFalse(task.done())
        self.assertTrue(all(item["status"] == 200 for item in self.server.poll_records(bridge.bridge_id)))
        listed = self.online_bridges()[bridge.bridge_id]
        self.assertEqual(listed["connection_state"], "connected")
        self.assertLess(listed["heartbeat_age_seconds"], 0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(diagnostics_cancelled.is_set(), "Connection exit must reap its diagnostic worker")

    async def test_256_finished_jobs_reconcile_without_reexecution_then_accept_new_work(self) -> None:
        bridge = self.make_bridge("history")

        async def diagnostics(**_kwargs: object) -> dict[str, Any]:
            return {}

        bridge._refresh_runtime_diagnostics = diagnostics
        registry = self.gateway._read_registry()
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        for index in range(256):
            job_id = f"devjob_{index:024x}"
            job = {
                "schema_version": "device_job/v1", "job_id": job_id,
                "request_id": f"req_{index:024x}", "project_id": "local-default",
                "bridge_id": bridge.bridge_id, "idempotency_key": f"history-{index}",
                "action": "device.execute_shell", "deadline": deadline,
                "payload": {"device_serial": "fake-phone", "command": "echo must-not-run"},
            }
            envelope = {
                "type": "job.result", "job_id": job_id, "request_id": job["request_id"],
                "bridge_id": bridge.bridge_id, "result": {"status": "succeeded", "execution": {"return_code": 0}},
            }
            registry["jobs"][job_id] = {"job": job, "state": "succeeded", "result": envelope}
            bridge._persist_job(job_id, {**job, "state": "succeeded", "result_envelope": envelope})
        self.gateway._write_registry(registry)
        executed: list[str] = []

        async def action(_transport: Any, job: dict[str, Any]) -> dict[str, Any]:
            executed.append(job["job_id"])
            return {"status": "succeeded", "execution": {"return_code": 0}}

        bridge._execute_action = action
        first_task = self.start_bridge(bridge)
        await self.wait_until(lambda: len(self.server.poll_records(bridge.bridge_id)) >= 3, timeout=6)
        first_poll = self.server.poll_records(bridge.bridge_id)[0]
        self.assertEqual(len(first_poll["payload"]["reconcile_jobs"]), 256)
        self.assertEqual(first_poll["response"]["messages"], [])
        self.assertLess(first_poll["elapsed"], 2)
        self.assertEqual(executed, [])
        self.assertEqual(self.online_bridges()[bridge.bridge_id]["connection_state"], "connected")
        # Wait for the true HTTP result replay, then reconnect the same local
        # spool: no historical device action may be re-offered or repeated.
        await self.wait_until(lambda: len(bridge._reported_result_job_ids) == 256, timeout=20)
        first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)
        previous_count = len(self.server.poll_records(bridge.bridge_id))
        self.start_bridge(bridge)
        await self.wait_until(lambda: len(self.server.poll_records(bridge.bridge_id)) > previous_count)
        second_first_poll = self.server.poll_records(bridge.bridge_id)[previous_count]
        self.assertNotEqual(first_poll["payload"]["connection_id"], second_first_poll["payload"]["connection_id"])
        self.assertTrue(second_first_poll["response"]["new_connection"])
        self.assertEqual(second_first_poll["response"]["messages"], [])

        lease = self.gateway.create_lease(
            project_id="local-default", bridge_id=bridge.bridge_id, logical_device_id="device_fake_phone",
        )
        job = self.gateway._bridge_job(
            project_id="local-default", agent_run_id="run_" + "a" * 24,
            bridge_id=bridge.bridge_id, lease_id=lease["lease_id"], logical_device_id="device_fake_phone",
            action="device.execute_shell", payload={"device_serial": "fake-phone", "command": "echo new-work"},
            idempotency_key="new-work-after-history", timeout_seconds=10,
        )
        result = await asyncio.to_thread(self.gateway.dispatch_bridge_job, job, wait_seconds=5)
        self.assertEqual(result["result"]["status"], "succeeded")
        self.assertEqual(executed, [job["job_id"]])

    async def test_slow_diagnostics_and_event_post_do_not_take_either_bridge_offline(self) -> None:
        slow = self.make_bridge("slow")
        fast = self.make_bridge("fast")
        diagnostics_started = asyncio.Event()
        diagnostics_release = asyncio.Event()
        event_started = threading.Event()
        self.server.slow_events[slow.bridge_id] = event_started

        async def slow_diagnostics(**_kwargs: object) -> dict[str, Any]:
            diagnostics_started.set()
            await diagnostics_release.wait()
            return {}

        async def fast_diagnostics(**_kwargs: object) -> dict[str, Any]:
            return {}

        slow._refresh_runtime_diagnostics = slow_diagnostics
        fast._refresh_runtime_diagnostics = fast_diagnostics
        slow._queue_http_event({"type": "bridge.heartbeat", "capabilities": slow.capabilities()})
        self.start_bridge(slow)
        self.start_bridge(fast)
        await self.wait_until(lambda: diagnostics_started.is_set() and event_started.is_set())
        await self.wait_until(lambda: all(len(self.server.poll_records(item.bridge_id)) >= 5 for item in (slow, fast)))
        self.assertFalse(self.server.release_events.is_set(), "Event response remains deliberately blocked")
        listed = self.online_bridges()
        for bridge in (slow, fast):
            self.assertEqual(listed[bridge.bridge_id]["connection_state"], "connected")
            self.assertLess(listed[bridge.bridge_id]["heartbeat_age_seconds"], 0.5)
            self.assertTrue(all(item["status"] == 200 for item in self.server.poll_records(bridge.bridge_id)))
        self.server.release_events.set()
        await self.wait_until(lambda: not slow._http_events)


if __name__ == "__main__":
    unittest.main()
