"""Command and collection budgets, using fake HDC and a harmless local stub."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from backend.tests.test_device_bridge_contract import BRIDGE_MODULE, HdcDeviceBridge, _BlockingProcess, _FakeProcess, _FakeWebSocket


class DeviceBridgeDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hdc = self.root / "hdc"
        self.hdc.write_text("stub")
        self.hdc.chmod(0o755)
        self.bridge = HdcDeviceBridge({"bridge_id": "bridge_test", "bridge_secret": "secret",
            "platform_http_url": "https://platform.example", "hdc_path": str(self.hdc),
            "spool_root": str(self.root / "spool")}, self.root / "bridge.json")

    def job(self, *, seconds=10, action="device.execute_shell", timeout=5):
        job = {"schema_version": "device_job/v1", "job_id": "devjob_" + "d" * 24,
            "request_id": "req_test", "bridge_id": self.bridge.bridge_id, "action": action,
            "deadline": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
            "payload": {"device_serial": "test-device", "command": "echo fake", "timeout_seconds": timeout,
                        "remote_path": "/data/local/tmp/layout.json"}}
        job["signature"] = hmac.new(self.bridge.bridge_secret.encode(), BRIDGE_MODULE._canonical_json(job), hashlib.sha256).hexdigest()
        return job

    async def test_collection_honors_short_timeout_and_preserves_large_default(self):
        for requested, expected in ((20, 20), (None, 600)):
            job = self.job(action="device.collect_artifact", timeout=requested)
            job.pop("deadline")
            if requested is None:
                job["payload"].pop("timeout_seconds")
            with self.subTest(requested=requested), patch.object(self.bridge, "_run_hdc", AsyncMock(return_value={"return_code": 1})) as run:
                await self.bridge._collect(job)
            observed = run.await_args.kwargs["timeout_seconds"]
            self.assertLessEqual(observed, expected)
            self.assertGreater(observed, expected - 1)

    async def test_queue_deadline_returns_without_launching_late_command(self):
        await self.bridge._hdc_lock.acquire()
        try:
            with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock()) as create:
                result = await self.bridge._run_hdc("list", "targets", timeout_seconds=.04, job_id="queued")
                self.assertTrue(result["timed_out"])
                self.assertEqual(result["error_code"], "BRIDGE_HDC_QUEUE_TIMEOUT")
                self.assertTrue(self.bridge._hdc_lock.locked())
                self.bridge._hdc_lock.release()
                await asyncio.sleep(.02)
                create.assert_not_awaited()
        finally:
            if self.bridge._hdc_lock.locked():
                self.bridge._hdc_lock.release()

    async def test_queue_wait_consumes_the_child_execution_budget(self):
        await self.bridge._hdc_lock.acquire()
        async def release():
            await asyncio.sleep(.04)
            self.bridge._hdc_lock.release()
        releaser = asyncio.create_task(release())
        with patch.object(self.bridge, "_run_hdc_unlocked", AsyncMock(return_value={"return_code": 0, "duration_ms": 0})) as run:
            result = await self.bridge._run_hdc("list", "targets", timeout_seconds=.2, job_id="shrinking")
        await releaser
        self.assertEqual(result["return_code"], 0)
        self.assertLess(run.await_args.kwargs["timeout_seconds"], .18)
        self.assertGreater(run.await_args.kwargs["timeout_seconds"], 0)
        self.assertGreaterEqual(result["queue_duration_ms"], 30)

    async def test_signed_job_expiring_in_queue_never_executes_after_lock_release(self):
        await self.bridge._hdc_lock.acquire()
        transport = _FakeWebSocket()
        try:
            with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock()) as create:
                await self.bridge._execute_job(transport, self.job(seconds=.05, timeout=30))
                self.assertEqual(transport.sent[-1]["result"]["status"], "timed_out")
                self.bridge._hdc_lock.release()
                await asyncio.sleep(.02)
                create.assert_not_awaited()
        finally:
            if self.bridge._hdc_lock.locked():
                self.bridge._hdc_lock.release()
        self.assertEqual(self.bridge._job_deadlines, {})
        self.assertEqual(self.bridge.processes, {})

    async def test_already_expired_job_never_reaches_action(self):
        transport = _FakeWebSocket()
        with patch.object(self.bridge, "_execute_action", AsyncMock()) as action:
            await self.bridge._execute_job(transport, self.job(seconds=-1))
        action.assert_not_awaited()
        self.assertEqual(transport.sent[-1]["result"]["stage"], "job_validation")

    async def test_deadline_stops_subsequent_command_after_non_hdc_work(self):
        transport = _FakeWebSocket()
        async def delayed_action(_transport, job):
            await asyncio.sleep(.2)
            return await self.bridge._execute_shell(job)
        with patch.object(self.bridge, "_execute_action", side_effect=delayed_action), \
             patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock()) as create:
            await self.bridge._execute_job(transport, self.job(seconds=.04))
        self.assertEqual(transport.sent[-1]["result"]["status"], "timed_out")
        create.assert_not_awaited()

    async def test_full_discovery_diagnostic_child_inherits_signed_deadline(self):
        process = _BlockingProcess()
        transport = _FakeWebSocket()
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[_FakeProcess(), process])) as create:
            await self.bridge._execute_job(transport, self.job(seconds=.05, action="device.discover"))
        self.assertEqual(transport.sent[-1]["result"]["status"], "timed_out")
        self.assertEqual(create.await_count, 2)
        self.assertTrue(process.killed)
        self.assertFalse(process.terminated)
        self.assertEqual(self.bridge.processes, {})

    async def test_cancel_wakes_queued_job_without_releasing_another_jobs_lock(self):
        await self.bridge._hdc_lock.acquire()
        try:
            with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock()) as create:
                task = asyncio.create_task(self.bridge._run_hdc("list", "targets", timeout_seconds=30, job_id="queued-cancel"))
                await asyncio.sleep(.01)
                await asyncio.wait_for(self.bridge._cancel_device_job("queued-cancel"), timeout=.5)
                result = await task
            self.assertTrue(result["cancelled"])
            self.assertTrue(self.bridge._hdc_lock.locked())
            create.assert_not_awaited()
        finally:
            self.bridge._hdc_lock.release()

    async def test_cancel_ack_waits_for_owned_child_cleanup(self):
        process = _BlockingProcess()
        self.bridge.processes["another-job"] = object()
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(self.bridge._run_hdc("list", "targets", timeout_seconds=30, job_id="cancel-active"))
            await process.communicate_started.wait()
            await self.bridge._cancel_device_job("cancel-active")
            self.assertIsNotNone(process.returncode)
            self.assertNotIn("cancel-active", self.bridge.processes)
            self.assertFalse(self.bridge._hdc_lock.locked())
            result = await task
        self.assertTrue(result["cancelled"])
        self.assertIn("another-job", self.bridge.processes)

    async def test_cancellation_during_spawn_reaps_child_before_releasing_slot(self):
        starting, release = asyncio.Event(), asyncio.Event()
        process = _BlockingProcess()
        async def create(*args, **kwargs):
            starting.set()
            await release.wait()
            return process
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", side_effect=create):
            task = asyncio.create_task(self.bridge._run_hdc("list", "targets", timeout_seconds=30, job_id="spawn-race"))
            await starting.wait()
            task.cancel()
            await asyncio.sleep(.01)
            self.assertTrue(self.bridge._hdc_lock.locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.killed)
        self.assertFalse(self.bridge._hdc_lock.locked())
        self.assertEqual(self.bridge.processes, {})

    async def test_device_cancel_waits_for_a_child_still_being_created(self):
        starting, release = asyncio.Event(), asyncio.Event()
        process = _BlockingProcess()
        async def create(*args, **kwargs):
            starting.set()
            await release.wait()
            return process
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", side_effect=create):
            task = asyncio.create_task(self.bridge._run_hdc("list", "targets", timeout_seconds=30, job_id="cancel-spawn"))
            await starting.wait()
            cancellation = asyncio.create_task(self.bridge._cancel_device_job("cancel-spawn"))
            await asyncio.sleep(.01)
            self.assertFalse(cancellation.done())
            release.set()
            await cancellation
            self.assertTrue(process.killed)
            self.assertFalse(self.bridge._hdc_lock.locked())
            self.assertTrue((await task)["cancelled"])

    async def test_subsecond_hdc_timeout_is_not_rounded_up_to_one_second(self):
        process = _BlockingProcess()
        started = time.monotonic()
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            result = await self.bridge._run_hdc("list", "targets", timeout_seconds=.04, job_id="short")
        self.assertTrue(result["timed_out"])
        self.assertTrue(process.killed)
        self.assertLess(time.monotonic() - started, .5)
        self.assertFalse(self.bridge._hdc_lock.locked())

    async def test_receive_and_upload_share_one_collection_budget(self):
        job = self.job(action="device.collect_artifact", timeout=.3)
        observed = []
        async def receive(*args, **kwargs):
            await asyncio.sleep(.04)
            Path(args[-1]).write_bytes(b"layout")
            return {"return_code": 0}
        def upload(request, *, timeout):
            observed.append(timeout)
            content = b"".join(request.data)
            return BytesIO(json.dumps({"status": "stored", "job_id": job["job_id"], "bytes": len(content),
                                     "sha256": hashlib.sha256(content).hexdigest()}).encode())
        with patch.object(self.bridge, "_run_hdc", side_effect=receive), patch.object(BRIDGE_MODULE, "_direct_urlopen", side_effect=upload):
            result = await self.bridge._collect(job)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertGreater(observed[0], 0)
        self.assertLess(observed[0], .28)

    async def test_collection_returns_on_budget_while_network_thread_finishes(self):
        job = self.job(action="device.collect_artifact", timeout=.06)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        async def receive(*args, **kwargs):
            Path(args[-1]).write_bytes(b"layout")
            return {"return_code": 0}
        def upload(request, *, timeout):
            started.set()
            try:
                release.wait(1)
                return BytesIO(b"{}")
            finally:
                finished.set()
        with patch.object(self.bridge, "_run_hdc", side_effect=receive), patch.object(BRIDGE_MODULE, "_direct_urlopen", side_effect=upload):
            try:
                result = await asyncio.wait_for(self.bridge._collect(job), timeout=.5)
                self.assertTrue(started.is_set())
                self.assertEqual(result["status"], "timed_out")
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 1))
                await asyncio.sleep(.01)

    @unittest.skipIf(os.name == "nt", "POSIX executable stub; no physical device")
    async def test_signed_shell_deadline_reaps_real_local_stub(self):
        self.hdc.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
        original = asyncio.create_subprocess_exec
        processes = []
        async def create(*args, **kwargs):
            process = await original(*args, **kwargs)
            processes.append(process)
            return process
        transport = _FakeWebSocket()
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", side_effect=create):
            await self.bridge._execute_job(transport, self.job(seconds=.12, timeout=20))
        self.assertEqual(transport.sent[-1]["result"]["status"], "timed_out")
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
        self.assertEqual(self.bridge.processes, {})
        self.assertFalse(self.bridge._hdc_lock.locked())
