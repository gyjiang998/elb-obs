"""Signed targets-only discovery deadlines, lock queueing and child cleanup."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from backend.tests.test_device_bridge_contract import BRIDGE_MODULE, HdcDeviceBridge, _BlockingProcess


class DeviceBridgeDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hdc = self.root / "hdc"
        self.hdc.write_text("stub")
        self.hdc.chmod(0o755)
        self.bridge = HdcDeviceBridge({"bridge_id": "bridge_test", "bridge_secret": "test-secret",
            "platform_http_url": "https://platform.example", "hdc_path": str(self.hdc),
            "spool_root": str(self.root / "spool")}, self.root / "config.json")

    def job(self, remaining=30):
        job = {"schema_version": "device_job/v1", "job_id": "devjob_targets_test", "bridge_id": "bridge_test",
               "action": "device.discover", "payload": {"targets_only": True},
               "deadline": (datetime.now(timezone.utc) + timedelta(seconds=remaining)).isoformat()}
        job["signature"] = hmac.new(self.bridge.bridge_secret.encode(), BRIDGE_MODULE._canonical_json(job), hashlib.sha256).hexdigest()
        return job

    async def test_targets_only_uses_one_command_and_labels_cached_diagnostics(self):
        cached = {"status": "ready", "checked_at": "earlier", "hdc": {"targets": {"stdout": "stale-phone"}}}
        self.bridge._runtime_diagnostics = cached
        fresh = {"return_code": 0, "stdout": "fresh-phone\n", "stderr": "", "timed_out": False}
        with patch.object(self.bridge, "_run_hdc", AsyncMock(return_value=fresh)) as run, \
             patch.object(self.bridge, "_refresh_runtime_diagnostics", AsyncMock()) as refresh:
            result = await self.bridge._discover(self.job())
        run.assert_awaited_once_with("list", "targets", timeout_seconds=10.0,
                                    job_id="devjob_targets_test", cancel_immediately=True)
        refresh.assert_not_awaited()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([device["device_serial"] for device in result["devices"]], ["fresh-phone"])
        self.assertIs(result["bridge_diagnostics"], cached)
        self.assertEqual(result["bridge_diagnostics_source"], "cached")
        self.assertEqual(result["discovery_profile"], "targets_only")
        self.assertIn("targets_only", self.bridge.capabilities()["discovery_profiles"])

    async def test_default_discovery_preserves_full_diagnostics_refresh(self):
        fresh = {"return_code": 0, "stdout": "phone\n", "stderr": ""}
        with patch.object(self.bridge, "_run_hdc", AsyncMock(return_value=fresh)) as run, \
             patch.object(self.bridge, "_refresh_runtime_diagnostics", AsyncMock(return_value={"status": "ready"})) as refresh:
            result = await self.bridge._discover({"job_id": "legacy-job", "payload": {}})
        run.assert_awaited_once_with("list", "targets", timeout_seconds=30, job_id="legacy-job")
        refresh.assert_awaited_once_with(targets_result=fresh, force=True, job_id="legacy-job")
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("discovery_profile", result)

    async def test_signed_remaining_deadline_limits_command_timeout(self):
        fresh = {"return_code": 0, "stdout": "", "stderr": ""}
        with patch.object(self.bridge, "_run_hdc", AsyncMock(return_value=fresh)) as run:
            result = await self.bridge._discover(self.job(.5))
        self.assertGreater(run.await_args.kwargs["timeout_seconds"], 0)
        self.assertLessEqual(run.await_args.kwargs["timeout_seconds"], .5)
        self.assertEqual(result["bridge_diagnostics_source"], "unavailable")

    async def test_expired_deadline_never_starts_hdc(self):
        with patch.object(self.bridge, "_run_hdc", AsyncMock()) as run:
            result = await self.bridge._discover(self.job(-1))
        run.assert_not_awaited()
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["hdc"]["error_code"], "BRIDGE_DISCOVERY_DEADLINE")

    async def test_waiting_for_another_jobs_hdc_lock_is_bounded(self):
        await self.bridge._hdc_lock.acquire()
        try:
            started = time.monotonic()
            with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock()) as create:
                result = await self.bridge._discover(self.job(.06))
            self.assertLess(time.monotonic() - started, .5)
            create.assert_not_awaited()
            self.assertTrue(self.bridge._hdc_lock.locked())
            self.assertEqual(result["status"], "timed_out")
            self.assertEqual(self.bridge.processes, {})
        finally:
            self.bridge._hdc_lock.release()

    async def test_running_discovery_deadline_kills_child_and_releases_lock(self):
        process = _BlockingProcess()
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            result = await self.bridge._discover(self.job(.06))
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(process.killed)
        self.assertFalse(process.terminated)
        self.assertFalse(self.bridge._hdc_lock.locked())
        self.assertEqual(self.bridge.processes, {})

    async def test_cancelled_running_discovery_reaps_only_its_child(self):
        process = _BlockingProcess()
        other_process = object()
        self.bridge.processes["other-job"] = other_process
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(self.bridge._discover(self.job()))
            await process.communicate_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.killed)
        self.assertFalse(self.bridge._hdc_lock.locked())
        self.assertEqual(self.bridge.processes, {"other-job": other_process})

    async def test_cancelled_queued_discovery_does_not_release_another_jobs_lock(self):
        await self.bridge._hdc_lock.acquire()
        try:
            with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock()) as create:
                task = asyncio.create_task(self.bridge._discover(self.job()))
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            create.assert_not_awaited()
            self.assertTrue(self.bridge._hdc_lock.locked())
        finally:
            self.bridge._hdc_lock.release()

    async def test_previously_cancelled_job_starts_no_child(self):
        self.bridge.cancelled_jobs.add("devjob_targets_test")
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock()) as create:
            result = await self.bridge._discover(self.job())
        create.assert_not_awaited()
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(self.bridge._hdc_lock.locked())

    async def test_process_start_failure_remains_a_failed_fresh_observation(self):
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock(side_effect=PermissionError("blocked"))), \
             patch.object(self.bridge, "_refresh_runtime_diagnostics", AsyncMock()) as refresh:
            result = await self.bridge._discover(self.job())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["hdc"]["launch_error"]["code"], "BRIDGE_HDC_PROCESS_START_FAILED")
        refresh.assert_not_awaited()
        self.assertEqual(self.bridge.processes, {})

    async def test_targets_only_flag_is_covered_by_job_signature(self):
        job = self.job()
        self.bridge._verify_job(job)
        job["payload"]["targets_only"] = False
        with self.assertRaisesRegex(ValueError, "signature invalid"):
            self.bridge._verify_job(job)

    @unittest.skipIf(os.name == "nt", "POSIX executable stub; device-independent")
    async def test_actual_local_stub_process_is_reaped_after_deadline(self):
        self.hdc.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
        original = asyncio.create_subprocess_exec
        processes = []
        async def tracked_create(*args, **kwargs):
            process = await original(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", side_effect=tracked_create):
            result = await self.bridge._discover(self.job(.15))
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
        self.assertEqual(self.bridge.processes, {})
        self.assertFalse(self.bridge._hdc_lock.locked())


if __name__ == "__main__":
    unittest.main()
