"""HTTP polling remains live while local probes or event delivery are slow."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import URLError

from backend.tests.test_device_bridge_contract import BRIDGE_MODULE, HdcDeviceBridge


class DeviceBridgeTransportErrorTests(unittest.TestCase):
    def test_connect_timeout_identifies_safe_endpoint_and_stage(self):
        with patch.object(BRIDGE_MODULE, "_direct_urlopen", side_effect=URLError(TimeoutError("private-token"))):
            with self.assertRaises(BRIDGE_MODULE.PlatformTransportError) as raised:
                BRIDGE_MODULE._http_json(
                    "https://private-user:private-password@platform.example:4032/v1/device-bridges/poll?token=private-query",
                    {"bridge_secret": "private-body"}, timeout=7,
                    extra_headers={"X-Bridge-Secret": "private-header"},
                )
        error = raised.exception
        self.assertEqual(error.endpoint, "https://platform.example:4032/v1/device-bridges/poll")
        self.assertEqual(error.phase, "connect_or_response_headers")
        self.assertEqual(error.exception_type, "TimeoutError")
        self.assertEqual(error.timeout_seconds, 7)
        self.assertGreaterEqual(error.elapsed_ms, 0)
        self.assertIn("timeout=7s", str(error))
        self.assertNotIn("private", str(error))

    def test_response_read_timeout_identifies_events_instead_of_polling(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = TimeoutError("private-response")
        with patch.object(BRIDGE_MODULE, "_direct_urlopen", return_value=response):
            with self.assertRaises(BRIDGE_MODULE.PlatformTransportError) as raised:
                BRIDGE_MODULE._http_json("http://platform.example/v1/device-bridges/events", {}, timeout=11)
        self.assertEqual(raised.exception.phase, "response_body")
        self.assertIn("/v1/device-bridges/events", str(raised.exception))
        self.assertNotIn("private-response", str(raised.exception))


class DeviceBridgeHttpWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        hdc = root / "hdc.exe"
        hdc.write_text("stub")
        self.bridge = HdcDeviceBridge({
            "bridge_id": "bridge_test", "bridge_secret": "private-secret",
            "platform_http_url": "https://platform.example", "hdc_path": str(hdc),
            "spool_root": str(root / "spool"),
        }, root / "bridge.json")

    def poll_response(self):
        return {
            "schema_version": "device_bridge_http_poll/v1",
            "protocol_version": BRIDGE_MODULE.HTTP_POLL_PROTOCOL_VERSION,
            "bridge_id": self.bridge.bridge_id, "messages": [], "poll_after_ms": 200,
        }

    def assert_workers_stopped(self):
        self.assertEqual([
            task.get_name() for task in asyncio.all_tasks()
            if task.get_name().startswith("bridge-http-") and not task.done()
        ], [])

    async def test_hung_diagnostics_do_not_block_initial_or_subsequent_poll(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        polls_finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        count = 0

        async def diagnose(**_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        def exchange(url, *_args, **_kwargs):
            nonlocal count
            self.assertTrue(url.endswith("/poll"))
            count += 1
            if count >= 3:
                loop.call_soon_threadsafe(polls_finished.set)
            return self.poll_response()

        with patch.object(self.bridge, "_refresh_runtime_diagnostics", side_effect=diagnose) as refresh, \
             patch.object(BRIDGE_MODULE, "_http_json", side_effect=exchange):
            task = asyncio.create_task(self.bridge._run_http_poll_connection())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(polls_finished.wait(), 2)
                self.assertEqual(refresh.await_count, 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assert_workers_stopped()

    async def test_hung_event_post_does_not_stop_heartbeat_or_drop_unacknowledged_result(self):
        job_id = "devjob_" + "a" * 24
        result = {"type": "job.result", "job_id": job_id, "bridge_id": self.bridge.bridge_id,
                  "result": {"status": "succeeded"}}
        self.bridge._persist_job(job_id, {"job_id": job_id, "state": "succeeded", "result_envelope": result})
        started = asyncio.Event()
        polls_finished = asyncio.Event()
        released = threading.Event()
        finished = threading.Event()
        loop = asyncio.get_running_loop()
        polls = 0
        posts = 0

        def exchange(url, *_args, **_kwargs):
            nonlocal polls, posts
            if url.endswith("/events"):
                posts += 1
                loop.call_soon_threadsafe(started.set)
                try:
                    released.wait(4)
                    return {"acknowledged": True}
                finally:
                    finished.set()
            polls += 1
            if polls >= 3:
                loop.call_soon_threadsafe(polls_finished.set)
            return self.poll_response()

        with patch.object(self.bridge, "_refresh_runtime_diagnostics", AsyncMock(return_value={})), \
             patch.object(BRIDGE_MODULE, "_http_json", side_effect=exchange):
            task = asyncio.create_task(self.bridge._run_http_poll_connection())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(polls_finished.wait(), 2)
                self.assertEqual(posts, 1)
                self.assertEqual(list(self.bridge._http_events), [result])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                released.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        self.assertEqual(list(self.bridge._http_events), [result])
        self.assertNotIn(job_id, self.bridge._reported_result_job_ids)
        self.assertTrue(self.bridge._job_path(job_id).is_file())
        self.assert_workers_stopped()

    async def test_event_failure_propagates_and_cleans_up_all_workers(self):
        event = {"type": "job.progress", "job_id": "devjob_test"}
        self.bridge._queue_http_event(event)
        error = BRIDGE_MODULE.PlatformTransportError(
            url="https://platform.example/v1/device-bridges/events", phase="response_body",
            timeout=30, elapsed_ms=30001, error=TimeoutError(),
        )

        def exchange(url, *_args, **_kwargs):
            if url.endswith("/events"):
                raise error
            return self.poll_response()

        with patch.object(self.bridge, "_refresh_runtime_diagnostics", AsyncMock(return_value={})), \
             patch.object(BRIDGE_MODULE, "_http_json", side_effect=exchange):
            with self.assertRaises(BRIDGE_MODULE.PlatformTransportError) as raised:
                await asyncio.wait_for(self.bridge._run_http_poll_connection(), 2)
        self.assertIs(raised.exception, error)
        self.assertEqual(list(self.bridge._http_events), [event])
        self.assert_workers_stopped()

    async def test_blocked_history_delivery_does_not_delay_live_discovery_results(self):
        historical_id = "devjob_" + "1" * 24
        fresh_id = "devjob_" + "2" * 24
        historical_result = {"type": "job.result", "job_id": historical_id,
            "bridge_id": self.bridge.bridge_id, "result": {"status": "succeeded"}}
        self.bridge._persist_job(historical_id, {"job_id": historical_id, "state": "succeeded",
            "result_envelope": historical_result})
        fresh_result = {"type": "job.result", "job_id": fresh_id,
            "bridge_id": self.bridge.bridge_id, "result": {"status": "succeeded",
                "devices": [{"device_serial": "PHONE", "connection_state": "connected"}]}}
        self.bridge._persist_job(fresh_id, {"job_id": fresh_id, "state": "running"})
        historical_started = asyncio.Event()
        live_delivered = asyncio.Event()
        release_history = threading.Event()
        historical_finished = threading.Event()
        loop = asyncio.get_running_loop()
        live_messages = []
        polls = 0

        def exchange(url, payload, **_kwargs):
            nonlocal polls
            if url.endswith("/poll"):
                polls += 1
                return self.poll_response()
            message = payload["message"]
            if message["job_id"] == historical_id:
                loop.call_soon_threadsafe(historical_started.set)
                try:
                    release_history.wait(4)
                finally:
                    historical_finished.set()
            else:
                live_messages.append(message)
                if message["type"] == "job.result":
                    loop.call_soon_threadsafe(live_delivered.set)
            return {"acknowledged": True}

        with patch.object(self.bridge, "_refresh_runtime_diagnostics", AsyncMock(return_value={})), \
             patch.object(BRIDGE_MODULE, "_http_json", side_effect=exchange):
            task = asyncio.create_task(self.bridge._run_http_poll_connection())
            try:
                await asyncio.wait_for(historical_started.wait(), 2)
                for message in ({"type": "job.accepted", "job_id": fresh_id},
                                {"type": "job.progress", "job_id": fresh_id, "progress": {"stage": "started"}},
                                fresh_result):
                    self.bridge._queue_http_event(message)
                await asyncio.wait_for(live_delivered.wait(), 1)
                # The fake HTTP server observes delivery before the sender has
                # resumed from to_thread and persisted its ACK. Wait for that
                # observable transition rather than assuming thread ordering.
                async def wait_for_live_ack():
                    while self.bridge.jobs[fresh_id].get("result_delivery", {}).get("state") != "acknowledged":
                        await asyncio.sleep(0)

                await asyncio.wait_for(wait_for_live_ack(), 1)
                self.assertFalse(release_history.is_set())
                self.assertFalse(historical_finished.is_set())
                self.assertEqual([message["type"] for message in live_messages],
                                 ["job.accepted", "job.progress", "job.result"])
                self.assertEqual(live_messages[-1]["result"]["devices"][0]["device_serial"], "PHONE")
                self.assertGreaterEqual(polls, 1)
                self.assertEqual(self.bridge.jobs[fresh_id]["result_delivery"]["state"], "acknowledged")
                self.assertEqual(self.bridge.jobs[fresh_id]["result_delivery"]["bridge_id"], self.bridge.bridge_id)
                release_history.set()
                self.assertTrue(await asyncio.to_thread(historical_finished.wait, 2))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                release_history.set()
                await asyncio.to_thread(historical_finished.wait, 2)
        self.assert_workers_stopped()

    async def test_acknowledged_result_remains_recoverable_in_a_new_process(self):
        job_id = "devjob_" + "3" * 24
        result = {"type": "job.result", "job_id": job_id, "bridge_id": self.bridge.bridge_id,
            "result": {"status": "succeeded"}}
        self.bridge._persist_job(job_id, {"job_id": job_id, "state": "succeeded", "result_envelope": result})
        self.bridge._queue_http_event(result)
        with patch.object(BRIDGE_MODULE, "_http_json", return_value={"acknowledged": True}):
            await self.bridge._flush_http_events("connection_test")
        self.assertEqual(self.bridge.jobs[job_id]["result_delivery"]["state"], "acknowledged")
        self.bridge._queue_completed_results()
        self.assertFalse(self.bridge._http_events, "Acknowledged result is deduplicated in this process")
        reloaded = HdcDeviceBridge(self.bridge.config, self.bridge.config_path)
        self.assertEqual(reloaded.jobs[job_id]["result_delivery"]["state"], "acknowledged")
        reloaded._queue_completed_results()
        self.assertEqual(list(reloaded._http_events), [result])
        self.assertIn(id(reloaded._http_events[0]), reloaded._http_replay_event_ids,
                      "A new process preserves server recovery through background-only replay")

    async def test_full_history_queue_reserves_space_for_live_results(self):
        for index in range(1024):
            self.bridge._queue_http_event({"type": "job.result", "job_id": f"devjob_{index:024x}"}, replay=True)
        live = {"type": "job.result", "job_id": "devjob_" + "f" * 24}
        self.bridge._queue_http_event(live)
        self.assertEqual(len(self.bridge._http_events), 1024)
        self.assertEqual(self.bridge._http_events[-1], live)
        self.assertNotIn(id(self.bridge._http_events[-1]), self.bridge._http_replay_event_ids)

    async def test_live_reoffer_promotes_history_without_duplicate_delivery(self):
        result = {"type": "job.result", "job_id": "devjob_" + "4" * 24,
                  "result": {"status": "succeeded"}}
        self.bridge._queue_http_event(result, replay=True)
        self.bridge._queue_http_event(result)
        with patch.object(BRIDGE_MODULE, "_http_json", return_value={"acknowledged": True}) as exchange:
            await asyncio.gather(self.bridge._flush_http_events("connection_test", replay=False),
                                 self.bridge._flush_http_events("connection_test", replay=True))
        exchange.assert_called_once()
        self.assertFalse(self.bridge._http_events)

    async def test_active_job_defers_diagnostics_without_accumulating_tasks(self):
        job_task = asyncio.create_task(asyncio.Event().wait())
        self.bridge._job_tasks["job_active"] = job_task
        polls_finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        count = 0

        def exchange(*_args, **_kwargs):
            nonlocal count
            count += 1
            if count >= 3:
                loop.call_soon_threadsafe(polls_finished.set)
            return self.poll_response()

        with patch.object(self.bridge, "_refresh_runtime_diagnostics", AsyncMock()) as refresh, \
             patch.object(BRIDGE_MODULE, "_http_json", side_effect=exchange):
            task = asyncio.create_task(self.bridge._run_http_poll_connection())
            try:
                await asyncio.wait_for(polls_finished.wait(), 2)
                refresh.assert_not_awaited()
                self.assertEqual(sum(t.get_name() == "bridge-http-diagnostics" for t in asyncio.all_tasks()), 1)
            finally:
                task.cancel()
                job_task.cancel()
                await asyncio.gather(task, job_task, return_exceptions=True)
        self.assert_workers_stopped()

    async def test_ack_of_shed_progress_cannot_remove_next_result(self):
        progress = {"type": "job.progress", "job_id": "devjob_progress"}
        result = {"type": "job.result", "job_id": "devjob_next"}
        self.bridge._queue_http_event(progress)

        async def exchange(*_args, **_kwargs):
            self.bridge._http_events.popleft()
            self.bridge._http_events.append(result)
            return {"acknowledged": True}

        with patch.object(BRIDGE_MODULE.asyncio, "to_thread", side_effect=exchange):
            await self.bridge._flush_http_events("connection_test", limit=1)
        self.assertEqual(list(self.bridge._http_events), [result])

    async def test_reconnect_log_reports_event_endpoint_without_secrets(self):
        error = BRIDGE_MODULE.PlatformTransportError(
            url="https://platform.example/v1/device-bridges/events", phase="response_body",
            timeout=30, elapsed_ms=30001, error=TimeoutError("private"),
        )
        output = StringIO()
        with patch.object(self.bridge, "_run_http_poll_connection", AsyncMock(side_effect=error)), \
             patch.object(BRIDGE_MODULE.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)), \
             redirect_stderr(output):
            with self.assertRaises(asyncio.CancelledError):
                await self.bridge.run()
        self.assertIn("HTTP transport temporarily unavailable", output.getvalue())
        self.assertIn("https://platform.example/v1/device-bridges/events", output.getvalue())
        self.assertIn("exception=TimeoutError", output.getvalue())
        self.assertNotIn("private", output.getvalue())
