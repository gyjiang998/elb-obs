from __future__ import annotations

from io import BytesIO
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.trace_library import trace_identity, bridge_copy_identity
from backend.tests.test_device_bridge_contract import BRIDGE_MODULE, HdcDeviceBridge


class BridgeTraceLibraryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.hdc = self.root / "hdc"
        self.hdc.write_text("stub")
        self.config = {"bridge_id": "bridge_test", "bridge_secret": "secret", "platform_http_url": "http://example.test",
            "hdc_path": str(self.hdc), "spool_root": str(self.root / "spool")}
        self.bridge = HdcDeviceBridge(self.config, self.root / "bridge.json")
        self.project = "test-project"
        self.run_id = "capture_" + "b" * 24
        self.relative = "07_edit/trace.ftrace"
        self.trace_id = trace_identity(self.project, self.run_id, self.relative)
        self.copy_id = bridge_copy_identity(self.project, "bridge_test", self.trace_id)
        self.content = b"original raw trace\n"
        self.job = {"job_id": "devjob_" + "c" * 24, "project_id": self.project, "payload": {
            "device_serial": "test-phone", "remote_path": "/data/local/tmp/trace.ftrace", "timeout_seconds": 600,
            "retain_trace": {"trace_id": self.trace_id, "copy_id": self.copy_id, "run_id": self.run_id,
                "relative_path": self.relative, "name": "trace.ftrace", "step_id": "step_07", "step_title": "点击编辑"}}}

    async def receive(self, *args, **kwargs):
        Path(args[-1]).write_bytes(self.content)
        return {"return_code": 0}

    def acknowledge(self, request, *, timeout):
        data = b"".join(request.data)
        self.assertEqual(data, self.content)
        return BytesIO(json.dumps({"status": "stored", "job_id": self.job["job_id"], "bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()}).encode())

    async def collect(self):
        with patch.object(self.bridge, "_run_hdc", new=self.receive), \
             patch.object(BRIDGE_MODULE, "_direct_urlopen", side_effect=self.acknowledge):
            return await self.bridge._collect(self.job)

    async def list_items(self, project=None):
        return await self.bridge._trace_library({"project_id": project or self.project, "action": "trace.library.list", "payload": {}})

    async def delete(self, copy_id=None, project=None, trace_id=None):
        return await self.bridge._trace_library({"project_id": project or self.project, "action": "trace.library.delete",
            "payload": {"trace_id": trace_id or self.trace_id, "copy_id": copy_id or self.copy_id}})

    async def test_success_keeps_named_raw_file_outside_spool_after_upload(self):
        result = await self.collect()
        self.assertEqual(result["status"], "succeeded", result)
        retained = result["retained_trace"]
        path = Path(retained["location"]["local_path"])
        self.assertEqual(path.name, "trace.ftrace")
        self.assertFalse(path.is_relative_to(self.bridge.spool_root))
        self.assertEqual(path.read_bytes(), self.content)
        self.assertFalse((self.bridge.spool_root / self.job["job_id"] / "collected.bin").exists())
        self.assertEqual(self.bridge._active_trace_ids, set())
        items = (await self.list_items())["items"]
        self.assertEqual(items[0]["location"]["status"], "available")
        self.assertEqual(items[0]["location"]["copy_id"], self.copy_id)

    async def test_upload_failure_still_preserves_windows_original(self):
        with patch.object(self.bridge, "_run_hdc", new=self.receive), \
             patch.object(BRIDGE_MODULE, "_direct_urlopen", side_effect=OSError("platform offline")):
            result = await self.bridge._collect(self.job)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(Path(result["retained_trace"]["location"]["local_path"]).read_bytes(), self.content)
        self.assertEqual(len((await self.list_items())["items"]), 1)
        self.assertEqual(self.bridge._active_trace_ids, set())

    async def test_retry_adopts_exact_orphan_raw_file_after_metadata_write_was_lost(self):
        metadata, raw = self.bridge._trace_paths(self.project, self.trace_id)
        raw.parent.mkdir(parents=True)
        raw.write_bytes(self.content)
        self.assertFalse(metadata.exists())

        result = await self.collect()
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(Path(result["retained_trace"]["location"]["local_path"]), raw)
        self.assertTrue(metadata.is_file())
        self.assertEqual(raw.read_bytes(), self.content)

    async def test_retry_never_overwrites_different_orphan_raw_file(self):
        metadata, raw = self.bridge._trace_paths(self.project, self.trace_id)
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"different retained trace\n")

        result = await self.collect()
        self.assertEqual(result["status"], "failed")
        self.assertIn("different data", result["error"])
        self.assertFalse(metadata.exists())
        self.assertEqual(raw.read_bytes(), b"different retained trace\n")

    async def test_delete_only_selected_raw_copy_and_preserve_metadata(self):
        result = await self.collect()
        path = Path(result["retained_trace"]["location"]["local_path"])
        note = path.parent / "notes.txt"
        note.write_text("keep")
        deleted = await self.delete()
        self.assertEqual(deleted["status"], "succeeded")
        self.assertFalse(path.exists())
        self.assertTrue(note.exists())
        self.assertTrue((path.parent / "metadata.json").exists())
        self.assertEqual((await self.list_items())["items"][0]["location"]["status"], "deleted")
        self.assertEqual((await self.delete())["status"], "succeeded")

    async def test_path_traversal_and_foreign_project_never_delete_trace(self):
        result = await self.collect()
        path = Path(result["retained_trace"]["location"]["local_path"])
        with self.assertRaises(ValueError):
            await self.delete(trace_id="../../secret")
        with self.assertRaises((ValueError, FileNotFoundError)):
            await self.delete(project="foreign")
        with self.assertRaises(ValueError):
            await self.delete(copy_id="copy_" + "d" * 24)
        self.assertTrue(path.exists())
        self.assertEqual((await self.list_items("foreign"))["items"], [])

    async def test_symlink_and_changed_raw_file_are_not_deleted(self):
        result = await self.collect()
        path = Path(result["retained_trace"]["location"]["local_path"])
        path.write_bytes(b"changed raw trace\n")
        with self.assertRaises(ValueError):
            await self.delete()
        self.assertTrue(path.exists())
        outside = self.root / "outside.ftrace"
        outside.write_bytes(self.content)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            await self.delete()
        self.assertTrue(outside.exists())

    async def test_in_progress_upload_cannot_be_deleted(self):
        result = await self.collect()
        self.bridge._active_trace_ids.add(self.trace_id)
        with self.assertRaisesRegex(ValueError, "active"):
            await self.delete()
        self.assertTrue(Path(result["retained_trace"]["location"]["local_path"]).exists())

    async def test_external_removal_is_missing_not_static_available(self):
        result = await self.collect()
        Path(result["retained_trace"]["location"]["local_path"]).unlink()
        self.assertEqual((await self.list_items())["items"][0]["location"]["status"], "missing")

    async def test_legacy_collection_preserves_transport_cleanup_without_retention(self):
        self.job["payload"].pop("retain_trace")
        result = await self.collect()
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("retained_trace", result)
        self.assertEqual((await self.list_items())["items"], [])

    async def test_retention_identity_is_verified_before_moving_file(self):
        self.job["payload"]["retain_trace"]["trace_id"] = "trace_" + "e" * 24
        result = await self.collect()
        self.assertEqual(result["status"], "failed")
        self.assertTrue((self.bridge.spool_root / self.job["job_id"] / "collected.bin").exists())
        self.assertEqual((await self.list_items())["items"], [])

    async def test_persistent_root_inside_transport_spool_is_rejected(self):
        with self.assertRaises(ValueError):
            HdcDeviceBridge({**self.config, "trace_root": str(self.root / "spool" / "raw-traces")}, self.root / "bridge.json")

    async def test_cancelled_upload_keeps_delete_guard_until_network_thread_stops(self):
        started, release = threading.Event(), threading.Event()
        def blocked_upload(request, *, timeout):
            started.set()
            release.wait(2)
            return self.acknowledge(request, timeout=timeout)
        with patch.object(self.bridge, "_run_hdc", new=self.receive), \
             patch.object(BRIDGE_MODULE, "_direct_urlopen", side_effect=blocked_upload):
            task = asyncio.create_task(self.bridge._collect(self.job))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIn(self.trace_id, self.bridge._active_trace_ids)
                with self.assertRaisesRegex(ValueError, "active"):
                    await self.delete()
            finally:
                release.set()
                for _ in range(100):
                    if self.trace_id not in self.bridge._active_trace_ids:
                        break
                    await asyncio.sleep(.01)
        self.assertNotIn(self.trace_id, self.bridge._active_trace_ids)

    async def test_list_paginates_without_running_hdc(self):
        await self.collect()
        result = await self.bridge._trace_library({"project_id": self.project, "action": "trace.library.list",
            "payload": {"limit": 1, "cursor": "0"}})
        self.assertEqual(len(result["items"]), 1)
        self.assertIsNone(result["next_cursor"])
        with self.assertRaises(ValueError):
            await self.bridge._trace_library({"project_id": self.project, "action": "trace.library.list", "payload": {"cursor": "../"}})


if __name__ == "__main__":
    unittest.main()
