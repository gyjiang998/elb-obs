"""Remote failure evidence uses synthetic devices, never a connected phone."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import shlex
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.core import ArtifactStore, PlatformError, atomic_write_json
from backend.parallel_workbench import create_parallel_router
from backend.scenario_capture import CaptureCancelled, GatewayDeviceClient, LocalDeviceClient, ScenarioCaptureManager, SCENARIO_BRIDGE_RESULT_GRACE_SECONDS
from backend.tests.test_scenario_capture import SimulatedDeviceClient
from tools.scenario_capture.gallery import CapturePreparationError
from tools.scenario_capture.runner import PREPARATION_SCREENSHOT_KIND, PREPARATION_SCREENSHOT_MAX_BYTES, execute_plan


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXGkAAAAASUVORK5CYII=")


class ScreenshotDevice(SimulatedDeviceClient):
    def __init__(self, *, failure="", image=PNG, remote_size=None):
        super().__init__()
        self.failure, self.image, self.remote_size = failure, image, remote_size
        self.cancelled = False
        self.screenshot_transfers = 0
        self.screenshot_options = None

    def check_cancelled(self):
        if self.cancelled:
            raise CaptureCancelled("cancelled by user")

    def shell(self, command, timeout=40, *, cleanup=False):
        result = super().shell(command, timeout, cleanup=cleanup)
        if command.startswith("uitest screenCap -p "):
            if self.failure == "command":
                raise RuntimeError("screenCap: permission denied")
            self.files[shlex.split(command)[-1]] = self.image
        if command.startswith("wc -c < "):
            return str(self.remote_size if self.remote_size is not None else len(self.files[shlex.split(command)[-1]]))
        return result

    def collect(self, remote, destination, *, timeout=600, cleanup=True):
        if remote.endswith("screen_preparation_failed.png"):
            self.screenshot_transfers += 1
            self.screenshot_options = {"timeout": timeout, "cleanup": cleanup}
            if self.failure == "transfer":
                raise RuntimeError("Bridge screenshot transfer failed")
        return super().collect(remote, destination, timeout=timeout, cleanup=cleanup)


class FailureScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), object())
        self.run_count = 0
        self.plan = self.manager.create_plan({"template_id": "custom", "steps": [
            {"title": "启动前准备", "operation": "wait", "seconds": 0}],
            "capture": {"selected_step_ids": ["step_01"], "collectors": ["trace"]}})

    @staticmethod
    def locked_error():
        return CapturePreparationError("SCENARIO_SCREEN_LOCKED", "设备仍停留在锁屏界面", "请查看失败现场")

    def run_failure(self, device, error=None):
        self.run_count += 1
        name = "run" if self.run_count == 1 else f"run_{self.run_count}"
        with patch("tools.scenario_capture.screen_preparation.prepare_unlocked_page", side_effect=error or self.locked_error()):
            return execute_plan(self.plan, device, self.root / name, "capture_test")

    def test_locked_failure_retains_png_and_layout_without_starting_collectors(self):
        device = ScreenshotDevice()
        manifest = self.run_failure(device)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["quality"]["errors"][0]["code"], "SCENARIO_SCREEN_LOCKED")
        self.assertEqual(device.executed, [])
        self.assertTrue(any(item["kind"] == "ui_layout" for item in manifest["files"]))
        screenshot = next(item for item in manifest["files"] if item["kind"] == PREPARATION_SCREENSHOT_KIND)
        self.assertEqual(screenshot["step_id"], "step_01")
        self.assertEqual(screenshot["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertEqual((self.root / "run" / screenshot["relative_path"]).read_bytes(), PNG)
        self.assertEqual(manifest["steps"][0]["screen_preparation_screenshot"]["status"], "retained")
        self.assertEqual(device.screenshot_transfers, 1)
        self.assertEqual(device.screenshot_options, {"timeout": 8, "cleanup": False})

    def test_screenshot_command_and_transfer_failure_preserve_original_error(self):
        for failure, reason in (("command", "permission denied"), ("transfer", "transfer failed")):
            with self.subTest(failure=failure):
                manifest = self.run_failure(ScreenshotDevice(failure=failure))
                self.assertEqual(manifest["quality"]["errors"][0]["reason"], "设备仍停留在锁屏界面")
                self.assertEqual(manifest["steps"][0]["error"]["code"], "SCENARIO_SCREEN_LOCKED")
                diagnostic = manifest["steps"][0]["screen_preparation_screenshot"]
                self.assertEqual(diagnostic["status"], "unavailable")
                self.assertIn(reason, diagnostic["reason"])
                self.assertFalse(any(item["kind"] == PREPARATION_SCREENSHOT_KIND for item in manifest["files"]))

    def test_usb_dismissal_failure_retains_remote_scene_and_original_error(self):
        device = ScreenshotDevice()
        manifest = self.run_failure(device, CapturePreparationError(
            "SCENARIO_USB_DIALOG", "USB 弹窗仍存在", "查看关闭记录"))
        self.assertEqual(manifest["steps"][0]["error"]["code"], "SCENARIO_USB_DIALOG")
        self.assertEqual(manifest["steps"][0]["screen_preparation_screenshot"]["status"], "retained")
        self.assertEqual(device.executed, [])
        self.assertEqual(device.screenshot_transfers, 1)

    def test_oversized_image_is_not_transferred_and_invalid_png_is_not_registered(self):
        for device in (ScreenshotDevice(remote_size=PREPARATION_SCREENSHOT_MAX_BYTES + 1), ScreenshotDevice(image=b"not a PNG image")):
            with self.subTest(size=device.remote_size):
                manifest = self.run_failure(device)
                self.assertFalse(any(item["kind"] == PREPARATION_SCREENSHOT_KIND for item in manifest["files"]))
                self.assertEqual(manifest["steps"][0]["screen_preparation_screenshot"]["status"], "unavailable")
                if device.remote_size:
                    self.assertEqual(device.screenshot_transfers, 0)

    def test_cancelled_capture_does_not_request_screenshot(self):
        device = ScreenshotDevice()
        manifest = self.run_failure(device, CaptureCancelled("cancelled"))
        self.assertEqual(manifest["status"], "cancelled")
        self.assertFalse(any(command.startswith("uitest screenCap") for command in device.commands))
        self.assertNotIn("screen_preparation_screenshot", manifest["steps"][0])

    def test_cancellation_before_evidence_preserves_failure_without_screenshot(self):
        device = ScreenshotDevice()
        def fail(*args, **kwargs):
            device.cancelled = True
            raise self.locked_error()
        with patch("tools.scenario_capture.screen_preparation.prepare_unlocked_page", side_effect=fail):
            manifest = execute_plan(self.plan, device, self.root / "cancelled", "capture_cancelled")
        self.assertEqual(manifest["quality"]["errors"][0]["code"], "SCENARIO_SCREEN_LOCKED")
        self.assertFalse(any(command.startswith("uitest screenCap") for command in device.commands))
        self.assertEqual(manifest["steps"][0]["screen_preparation_screenshot"]["status"], "skipped")

    def test_unrelated_failure_and_post_capture_lock_do_not_request_screenshot(self):
        device = ScreenshotDevice()
        self.run_failure(device, RuntimeError("transport unavailable"))
        self.assertFalse(any(command.startswith("uitest screenCap") for command in device.commands))
        device = ScreenshotDevice()
        with patch("tools.scenario_capture.runner.wait_for_step_ready", side_effect=self.locked_error()):
            manifest = execute_plan(self.plan, device, self.root / "after", "capture_after")
        self.assertTrue(device.executed)
        self.assertEqual(manifest["status"], "failed")
        self.assertFalse(any(command.startswith("uitest screenCap") for command in device.commands))


class FailureScreenshotDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ArtifactStore(self.root, project_id="screenshots-test")
        self.manager = ScenarioCaptureManager(self.root, self.store, object())
        self.run_id = "capture_" + "a" * 24
        self.directory = self.manager._run_dir(self.run_id)
        self.image_path = self.directory / "01_action_01" / "screen_preparation_failed.png"
        self.image_path.parent.mkdir(parents=True)
        self.image_path.write_bytes(PNG)
        self.entry = {"step_id": "step_01", "kind": PREPARATION_SCREENSHOT_KIND,
                      "relative_path": "01_action_01/screen_preparation_failed.png",
                      "size_bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}
        self.run = {"run_id": self.run_id, "project_id": self.store.project_id,
                    "status": "failed", "manifest": {"files": [self.entry]}}
        self.save()
        app = FastAPI()
        @app.exception_handler(PlatformError)
        async def failure(request: Request, exc: PlatformError):
            return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status_code)
        app.include_router(create_parallel_router(SimpleNamespace(capture=self.manager)))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.url = f"/v1/scenario-capture/runs/{self.run_id}/steps/step_01/preparation-screenshot"

    def save(self):
        atomic_write_json(self.directory / "run.json", self.run)

    def test_view_and_download_return_registered_png_with_integrity_header(self):
        for suffix, disposition in (("", "inline"), ("?download=true", "attachment")):
            response = self.client.get(self.url + suffix)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, PNG)
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertTrue(response.headers["Content-Disposition"].startswith(disposition))
            self.assertEqual(response.headers["X-Artifact-Sha256"], self.entry["sha256"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_unregistered_file_or_wrong_step_or_project_is_not_served(self):
        self.assertEqual(self.client.get(self.url.replace("step_01", "step_02")).status_code, 404)
        self.entry["kind"] = "trace"
        self.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.entry["kind"] = PREPARATION_SCREENSHOT_KIND
        self.run["project_id"] = "another-project"
        self.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_changed_or_deleted_image_is_not_served(self):
        self.image_path.write_bytes(PNG + b"changed")
        self.assertEqual(self.client.get(self.url).status_code, 409)
        self.image_path.unlink()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_path_traversal_and_symlink_are_rejected(self):
        original = self.entry["relative_path"]
        for path in ("../../screen_preparation_failed.png", str(self.image_path), "01_action_01/other.png"):
            self.entry["relative_path"] = path
            self.save()
            self.assertEqual(self.client.get(self.url).status_code, 422)
        self.entry["relative_path"] = original
        self.save()
        external = self.root / "external.png"
        external.write_bytes(PNG)
        self.image_path.unlink()
        self.image_path.symlink_to(external)
        self.assertEqual(self.client.get(self.url).status_code, 409)

    def test_non_png_even_with_matching_digest_is_not_served(self):
        bad = b"<html>bad</html>"
        self.image_path.write_bytes(bad)
        self.entry.update(size_bytes=len(bad), sha256=hashlib.sha256(bad).hexdigest())
        self.save()
        self.assertEqual(self.client.get(self.url).status_code, 409)


class ScreenshotTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def client(self, mode, *, behavior="success"):
        flag, interrupted = threading.Event(), threading.Event()
        calls = []
        digest = hashlib.sha256(PNG).hexdigest()

        def perform():
            if behavior == "timeout":
                raise TimeoutError("screenshot transfer timed out")
            if behavior == "cancel":
                flag.set()
                if not interrupted.wait(2):
                    raise AssertionError("screenshot cancellation did not reach transport")
                return {"status": "cancelled"}
            return {"status": "succeeded", "sha256": digest}

        def dispatch(job, **kwargs):
            calls.append({"job": job, **kwargs})
            result = perform()
            if result["status"] == "succeeded":
                source = self.root / "device-gateway" / "bridge-collected" / f"{job['job_id']}-{digest[:16]}.bin"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(PNG)
            return {"result": result}

        def local_collect(**kwargs):
            calls.append(kwargs)
            result = perform()
            if result["status"] == "succeeded":
                kwargs["destination"].write_bytes(PNG)
            return result

        gateway = SimpleNamespace(
            logical_device_id=lambda serial: "phone",
            create_lease=lambda **kwargs: {"lease_id": "lease"},
            ensure_active_lease=lambda *args, **kwargs: {"effective_lease_id": "lease"},
            _bridge_job=lambda **kwargs: {"job_id": "devjob_test", **kwargs},
            dispatch_bridge_job=dispatch,
            cancel_bridge_job=lambda **kwargs: interrupted.set(),
            local_collect=local_collect,
            local_executor=SimpleNamespace(cancel_run=lambda run_id: interrupted.set()),
        )
        common = {"device_serial": "phone", "run_id": "capture_" + "b" * 24,
                  "run_dir": self.root, "cancel_event": flag}
        client = (GatewayDeviceClient(gateway, state_root=self.root, project_id="test", bridge_id="bridge", **common)
                  if mode == "bridge" else LocalDeviceClient(gateway, **common))
        return client, calls, flag, interrupted

    def test_screenshot_timeout_reaches_bridge_job_and_local_executor(self):
        for mode in ("bridge", "local"):
            with self.subTest(mode=mode):
                client, calls, _, _ = self.client(mode)
                client.collect("/data/local/tmp/screen.png", self.root / f"{mode}.png", timeout=20, cleanup=False)
                if mode == "bridge":
                    self.assertEqual(calls[0]["wait_seconds"], 20 + SCENARIO_BRIDGE_RESULT_GRACE_SECONDS)
                    self.assertEqual(calls[0]["job"]["timeout_seconds"], 20)
                    self.assertEqual(calls[0]["job"]["payload"]["timeout_seconds"], 20)
                else:
                    self.assertEqual(calls[0]["timeout_seconds"], 20)
                    self.assertTrue(calls[0]["agent_run_id"].endswith(":capture"))
                    self.assertFalse(client.jobs[0]["cleanup"])

    def test_bridge_and_local_screenshot_transfer_can_be_cancelled_while_waiting(self):
        for mode in ("bridge", "local"):
            with self.subTest(mode=mode):
                client, _, _, interrupted = self.client(mode, behavior="cancel")
                with self.assertRaises(CaptureCancelled):
                    client.collect("/data/local/tmp/screen.png", self.root / f"{mode}.png", timeout=20, cleanup=False)
                self.assertTrue(interrupted.is_set())

    def test_timeout_is_preserved_and_trace_cleanup_default_is_unchanged(self):
        for mode in ("bridge", "local"):
            with self.subTest(mode=mode):
                client, _, _, _ = self.client(mode, behavior="timeout")
                with self.assertRaisesRegex(TimeoutError, "transfer timed out"):
                    client.collect("/data/local/tmp/screen.png", self.root / f"{mode}.png", timeout=20, cleanup=False)
                client, calls, flag, _ = self.client(mode)
                flag.set()
                client.collect("/data/local/tmp/evidence.bin", self.root / f"{mode}.bin")
                self.assertEqual(calls[0]["wait_seconds"] if mode == "bridge" else calls[0]["timeout_seconds"],
                    600 + SCENARIO_BRIDGE_RESULT_GRACE_SECONDS if mode == "bridge" else 600)
                if mode == "local":
                    self.assertTrue(calls[0]["agent_run_id"].endswith(":cleanup"))


if __name__ == "__main__":
    unittest.main()
