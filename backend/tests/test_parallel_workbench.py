from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.core import ArtifactStore, PlatformError, atomic_write_json
from backend.parallel_workbench import ParallelWorkbench, TERMINAL, create_parallel_router
from backend.parallel_modeling.manager import HARDWARE_ID
from backend.tests.test_parallel_modeling import trace_fixture as model_trace
from backend.tests.test_scenario_capture import SimulatedDeviceClient


class StubGateway:
    def __init__(self):
        self.projects = []
        self.connected = True

    def list_bridges(self, *, project_id):
        self.projects.append(project_id)
        return [{"bridge_id": "bridge_test", "connection_state": "connected" if self.connected else "offline"}]


class ModelingDevice(SimulatedDeviceClient):
    """Retains real runner output with a synthetic, semantic UI callback fixture."""
    def shell(self, command, timeout=40, *, cleanup=False):
        result = super().shell(command, timeout, cleanup=cleanup)
        if command.startswith("sh "):
            root = str(Path(shlex.split(command)[1]).parent)
            if root + "/trace.ftrace" in self.files:
                prefix = ("shell-100 [000] .... 0.998000: tracing_mark_write: B|100|GALLERY_STEP_02_action_02\n"
                          "shell-100 [000] .... 0.999000: tracing_mark_write: B|100|GALLERY_ACTION_02\n")
                suffix = ("shell-100 [000] .... 1.008000: tracing_mark_write: E|100\n"
                          "shell-100 [000] .... 1.009000: tracing_mark_write: B|100|GALLERY_HOLD_02\n"
                          "shell-100 [000] .... 2.009000: tracing_mark_write: E|100\n"
                          "shell-100 [000] .... 2.010000: tracing_mark_write: E|100\n")
                self.files[root + "/trace.ftrace"] = (prefix + model_trace(binder=True) + suffix).encode()
        return result


class ParallelWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = ArtifactStore(self.root, project_id="test-project")
        self.gateway = StubGateway()
        self.service = ParallelWorkbench(self.root, self.store, self.gateway)
        self.addCleanup(self.service.shutdown)
        self.environment = patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "device"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        app = FastAPI()

        @app.exception_handler(PlatformError)
        async def platform_error(request: Request, exc: PlatformError):
            return JSONResponse({"error": {"code": exc.code, "message": str(exc), "details": exc.details}}, status_code=exc.status_code)

        app.include_router(create_parallel_router(self.service))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def wait_job(self, job, *, expected=None):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = self.service.get_job(job["job_id"])
            if value["status"] in TERMINAL:
                if expected is not None:
                    self.assertEqual(value["status"], expected, value)
                self.assertIn("finished_at", value)
                return value
            time.sleep(.005)
        self.fail("Background test job did not finish")

    def plan(self, *, pmu=False):
        return self.service.capture.create_plan({"template_id": "custom", "steps": [
            {"title": "prepare", "operation": "wait", "seconds": 0},
            {"title": "capture", "operation": "wait", "seconds": 0},
            {"title": "do not execute", "operation": "wait", "seconds": 0}],
            "capture": {"selected_step_ids": ["step_02"], "collectors": ["trace", "pmu"] if pmu else ["trace"]}})

    def test_catalog_project_mode_and_bridge_preflight(self):
        response = self.client.get("/v1/scenario-capture/catalog")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.gateway.projects[-1], "test-project")
        plan = self.plan()
        for bridge, connected, code in [("missing", True, "SCENARIO_BRIDGE_NOT_FOUND"), ("bridge_test", False, "SCENARIO_BRIDGE_OFFLINE")]:
            self.gateway.connected = connected
            response = self.client.post(f'/v1/scenario-capture/plans/{plan["plan_id"]}/execute', json={"bridge_id": bridge, "device_serial": "stub"})
            self.assertEqual(response.json()["error"]["code"], code)
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "analysis"}):
            with self.assertRaises(PlatformError) as caught:
                self.service.action("scenario_capture.discover_devices", {"bridge_id": "bridge_test"})
            self.assertEqual(caught.exception.code, "DEPLOYMENT_DEVICE_VALIDATION_DISABLED")
        self.assertEqual(list(self.service.root.glob("*.json")), [])

    def test_script_download_and_agent_json_contract(self):
        plan = self.plan()
        response = self.client.get(f'/v1/scenario-capture/plans/{plan["plan_id"]}/script')
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/x-python", response.headers["content-type"])
        self.assertIn('attachment; filename="capture_scenario.py"', response.headers["content-disposition"])
        result = self.service.action("scenario_capture.generate_script", {"plan_id": plan["plan_id"]})
        self.assertEqual(response.text, result["content"])
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), result["sha256"])
        ast.parse(response.text)
        self.assertIn("/v1/parallel-jobs/", response.text)
        self.assertIn('"step_02"', response.text)
        for action, data in [("scenario_capture.read_plan", {}), ("unknown.action", {})]:
            with self.assertRaises(PlatformError) as caught:
                self.service.action(action, data)
            self.assertEqual(caught.exception.status_code, 422)

    def test_local_package_download_without_selected_device_needs_no_discovery(self):
        plan = self.plan()
        self.gateway.connected = False
        with patch.object(self.service.capture, "discover_devices") as discover:
            response = self.client.get(f'/v1/scenario-capture/plans/{plan["plan_id"]}/local-package')
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(response.headers["content-type"], "application/zip")
        discover.assert_not_called()

    def test_local_discovery_and_submission_need_no_bridge(self):
        self.gateway.connected = False
        plan = self.plan()
        with patch.object(self.service.capture, "catalog", return_value={"local_hdc": {"available": True}}), \
             patch.object(self.service.capture, "discover_devices", return_value={"devices": [{"serial": "local_stub"}]}) as discover, \
             patch.object(self.service.capture, "execute", return_value={"status": "completed"}) as execute:
            found = self.client.post('/v1/scenario-capture/devices/discover', json={"connection_mode": "local"})
            self.assertEqual(found.status_code, 200, found.text)
            self.assertEqual(found.json()["devices"], [{"serial": "local_stub"}])
            discover.assert_called_once_with("", connection_mode="local")
            response = self.client.post(f'/v1/scenario-capture/plans/{plan["plan_id"]}/execute',
                json={"connection_mode": "local", "device_serial": "local_stub", "bridge_id": "stale_bridge", "lease_id": "stale_lease"})
            self.assertEqual(response.status_code, 200, response.text)
            job = self.wait_job(response.json(), expected="succeeded")
            self.assertEqual(job["inputs"]["connection_mode"], "local")
            self.assertEqual(job["inputs"]["bridge_id"], "")
            self.assertEqual(execute.call_args.kwargs["connection_mode"], "local")
            self.assertEqual(execute.call_args.kwargs["device_serial"], "local_stub")
            self.assertEqual(execute.call_args.kwargs["bridge_id"], "")
            self.assertEqual(execute.call_args.kwargs["lease_id"], "")
            self.assertEqual(self.gateway.projects, [])

    def test_connection_mode_validation_and_local_hdc_preflight(self):
        plan = self.plan()
        for mode in ("invalid", None, [], {}):
            for path in ('/v1/scenario-capture/devices/discover', f'/v1/scenario-capture/plans/{plan["plan_id"]}/execute'):
                response = self.client.post(path, json={"connection_mode": mode, "device_serial": "stub"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"]["code"], "SCENARIO_CONNECTION_MODE_INVALID")
        with patch.object(self.service.capture, "catalog", return_value={"local_hdc": {"available": False}}), \
             patch.object(self.service.capture, "execute") as execute, \
             patch.object(self.service.capture, "discover_devices") as discover:
            response = self.client.post(f'/v1/scenario-capture/plans/{plan["plan_id"]}/execute', json={"connection_mode": "local", "device_serial": "stub"})
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["error"]["code"], "SCENARIO_LOCAL_HDC_UNAVAILABLE")
            execute.assert_not_called()
            discover.assert_not_called()
        self.assertEqual(list(self.service.root.glob("*.json")), [])

    def test_local_device_reservation_survives_bridge_selection_changes(self):
        plan = self.plan()
        started = threading.Event()

        def execute(plan_id, *, cancel_event, **options):
            started.set()
            cancel_event.wait(3)
            return {"status": "cancelled"}

        with patch.object(self.service.capture, "catalog", return_value={"local_hdc": {"available": True}}), \
             patch.object(self.service.capture, "execute", side_effect=execute):
            job = self.service.execute_capture(plan["plan_id"], {"connection_mode": "local", "device_serial": "stub"})
            self.assertTrue(started.wait(2))
            self.assertEqual(self.service._device_jobs[("local", "stub")], job["job_id"])
            with self.assertRaises(PlatformError) as caught:
                self.service.execute_capture(plan["plan_id"], {"connection_mode": "local", "device_serial": "stub", "bridge_id": "stale_bridge"})
            self.assertEqual(caught.exception.code, "SCENARIO_DEVICE_BUSY")
            self.service.cancel_job(job["job_id"])
            self.wait_job(job, expected="cancelled")
        self.assertEqual(self.service._device_jobs, {})

    def test_job_success_partial_failure_and_exception_are_durable(self):
        for status, expected in [("completed", "succeeded"), ("partial", "partial"), ("failed", "failed"), ("cancelled", "cancelled")]:
            def operation(*, progress, cancel_event, status=status):
                update = {"stage": "working"}
                progress(update)
                update["stage"] = "mutated afterward"
                return {"status": status, "evidence": [1, 2]}
            result = self.wait_job(self.service.submit("scenario_capture", operation, inputs={}), expected=expected)
            self.assertEqual(result["progress"], {"stage": "working"})
            self.assertEqual(result["result"]["evidence"], [1, 2])

        def fail(**callbacks):
            raise PlatformError("SPECIFIC_FAILURE", "test error", details={"item": 1})
        failed = self.wait_job(self.service.submit("parallel_search", fail, inputs={}), expected="failed")
        self.assertEqual(failed["error"], {"code": "SPECIFIC_FAILURE", "message": "test error", "details": {"item": 1}})

    def test_running_capture_reserves_device_until_cancellation_cleanup(self):
        plan = self.plan()
        started, cleaned = threading.Event(), threading.Event()

        def execute(plan_id, *, cancel_event, progress, **options):
            started.set()
            cancel_event.wait(3)
            cleaned.set()
            return {"status": "cancelled", "run_id": options["run_id"]}

        with patch.object(self.service.capture, "execute", side_effect=execute):
            job = self.service.execute_capture(plan["plan_id"], {"bridge_id": "bridge_test", "device_serial": "stub"})
            self.assertTrue(started.wait(2))
            with self.assertRaises(PlatformError) as caught:
                self.service.execute_capture(plan["plan_id"], {"bridge_id": "bridge_test", "device_serial": "stub"})
            self.assertEqual(caught.exception.code, "SCENARIO_DEVICE_BUSY")
            self.assertEqual(caught.exception.details["job_id"], job["job_id"])
            self.service.cancel_job(job["job_id"])
            self.wait_job(job, expected="cancelled")
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.service._device_jobs, {})

    def test_capture_job_retains_actionable_failure_instead_of_generic_message(self):
        failure = {"code": "SCENARIO_SCREEN_LOCKED", "reason": "手机当前处于锁屏界面。", "suggestion": "解锁手机后重新执行采集。", "step_id": "step_01", "step_title": "启动图库", "phase": "preparation"}
        result = {"status": "failed", "error": None, "manifest": {"quality": {"errors": [failure]}, "steps": [{"status": "failed", "step_id": "step_01", "title": "启动图库"}]}}
        job = self.wait_job(self.service.submit("scenario_capture", lambda **_: result, inputs={}), expected="failed")
        response = self.client.get('/v1/parallel-jobs/' + job["job_id"])
        self.assertEqual(response.status_code, 200)
        error = response.json()["error"]
        self.assertEqual(error["code"], failure["code"])
        self.assertEqual(error["message"], failure["reason"])
        self.assertEqual(error["details"]["suggestion"], failure["suggestion"])
        self.assertEqual(error["details"]["step_id"], "step_01")

    def test_queued_capture_run_cancel_does_not_execute_and_releases_reservation(self):
        release = threading.Event()
        self.addCleanup(release.set)
        started = [threading.Event(), threading.Event()]

        def occupy(index):
            def operation(**callbacks):
                started[index].set()
                release.wait(3)
                return {"status": "completed"}
            return operation

        workers = [self.service.submit("test", occupy(i), inputs={}) for i in range(2)]
        self.assertTrue(all(event.wait(2) for event in started))
        plan = self.plan()
        with patch.object(self.service.capture, "execute") as execute:
            job = self.service.execute_capture(plan["plan_id"], {"bridge_id": "bridge_test", "device_serial": "stub"})
            response = self.client.get('/v1/scenario-capture/runs/' + job["run_id"])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "queued")
            self.assertEqual(response.json()["job_id"], job["job_id"])
            with self.assertRaises(PlatformError) as caught:
                self.service.import_dataset({"capture_run_id": job["run_id"]})
            self.assertEqual(caught.exception.code, "CAPTURE_NOT_FINISHED")
            cancelled = self.service.cancel_job(job["job_id"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(self.service._device_jobs, {})
            release.set()
            for worker in workers:
                self.wait_job(worker, expected="succeeded")
            self.service.shutdown()
            execute.assert_not_called()
        self.assertEqual(self.service.get_capture_run(job["run_id"])["status"], "cancelled")

    def test_restart_marks_unfinished_jobs_and_runs_interrupted(self):
        run_id = "capture_" + "a" * 24
        for index, status in enumerate(["queued", "running", "partial", "succeeded"]):
            job_id = "parallel_job_" + f"{index:024x}"
            value = {"job_id": job_id, "status": status, "inputs": {"plan_id": "test"}, "created_at": "test"}
            if status == "running":
                value["run_id"] = run_id
            atomic_write_json(self.service.root / (job_id + ".json"), value)
        (self.service.root / ("parallel_job_" + "f" * 24 + ".json")).write_text("truncated")
        restarted = ParallelWorkbench(self.root, self.store, self.gateway)
        self.addCleanup(restarted.shutdown)
        self.assertEqual(restarted.get_job("parallel_job_" + f"{0:024x}")["status"], "queued")
        restarted.recover_after_restart()
        for index in range(2):
            value = restarted.get_job("parallel_job_" + f"{index:024x}")
            self.assertEqual(value["status"], "failed")
            self.assertEqual(value["error"]["code"], "JOB_INTERRUPTED")
        self.assertEqual(restarted.get_job("parallel_job_" + f"{2:024x}")["status"], "partial")
        self.assertEqual(restarted.get_capture_run(run_id)["status"], "failed")

    def test_real_capture_manifest_import_analysis_and_search_over_http(self):
        plan = self.plan(pmu=True)
        device = ModelingDevice()
        hardware_payload = self.service.modeling._builtin_hardware_profile()
        hardware_payload.update(name="stub 设备画像", source="local_hdc",
                                evidence={"device_serial_sha256": hashlib.sha256(b"stub").hexdigest()})
        hardware_payload.pop("id", None)
        hardware = self.service.modeling.create_hardware_profile(hardware_payload)
        execute = self.service.capture.execute
        with patch.object(self.service.capture, "execute", side_effect=lambda *args, **kwargs: execute(*args, **kwargs, device_client=device)):
            response = self.client.post(f'/v1/scenario-capture/plans/{plan["plan_id"]}/execute', json={"bridge_id": "bridge_test", "device_serial": "stub"})
            self.assertEqual(response.status_code, 200, response.text)
            job = self.wait_job(response.json(), expected="partial")
        run = self.client.get('/v1/scenario-capture/runs/' + job["run_id"]).json()
        self.assertEqual(run["manifest"]["steps"][1]["collector_status"]["pmu"], "unavailable")
        self.assertTrue(run["manifest_artifact_id"])
        trace = next(row for row in run["manifest"]["files"] if row["kind"] == "trace")
        self.assertTrue((self.root / trace["managed_path"]).is_file())
        # Import must use the immutable manifest, not a tampered mutable run snapshot.
        run_path = self.root / "scenario-capture" / "runs" / run["run_id"] / "run.json"
        mutable_run = json.loads(run_path.read_text())
        mutable_run["manifest"] = {"tampered": True}
        atomic_write_json(run_path, mutable_run)
        response = self.client.post('/v1/parallel-modeling/datasets/import', json={"capture_run_id": run["run_id"], "step_id": "step_02", "hardware_profile_id": hardware["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        dataset = response.json()
        self.assertEqual(dataset["summary"]["duration_ms"], 5.0)
        self.assertEqual(dataset["provenance"]["capture"]["run_id"], run["run_id"])
        self.assertEqual(dataset["hardware_profile_id"], hardware["id"])
        self.assertEqual(dataset["provenance"]["hardware_resolution"]["status"], "matched_import_request_profile")
        analysis_job = self.client.post('/v1/parallel-modeling/analyses', json={"dataset_id": dataset["id"]}).json()
        analysis = self.wait_job(analysis_job, expected="succeeded")["result"]
        self.assertTrue(analysis["replay_gate"]["passed"])
        self.assertEqual(analysis["baseline"]["service_call_count"], 1)
        search_job = self.client.post('/v1/parallel-modeling/searches', json={"analysis_id": analysis["id"], "space": {
            "frequency_policy": "max_opps", "worker_counts": [1], "available_core_group_ids": ["big", "middle"]}}).json()
        search = self.wait_job(search_job, expected="succeeded")["result"]
        self.assertEqual(search["best"]["data_kind"], "conditional_prediction")
        self.assertGreater(search["best"]["metrics"]["finish_ms"], 0)
        self.assertEqual(self.client.get('/v1/parallel-modeling/searches/' + search["id"]).json()["id"], search["id"])

    def test_archived_evidence_full_http_pipeline(self):
        dataset = self.client.post('/v1/parallel-modeling/datasets', json={"builtin_id": "gallery-edit-pmu-20261014"}).json()
        analysis = self.wait_job(self.client.post('/v1/parallel-modeling/analyses', json={"dataset_id": dataset["id"]}).json(), expected="succeeded")["result"]
        self.assertAlmostEqual(analysis["baseline"]["replay_ms"], 86.940, places=6)
        search = self.wait_job(self.client.post('/v1/parallel-modeling/searches', json={"analysis_id": analysis["id"], "space": {
            "frequency_policy": "max_opps", "worker_counts": [3], "available_core_group_ids": ["big", "middle"]}}).json(), expected="succeeded")["result"]
        self.assertAlmostEqual(search["best"]["metrics"]["finish_ms"], 53.87303045981391, places=6)
        self.assertEqual(search["model_package"]["status"], "legacy_compat")


if __name__ == "__main__":
    unittest.main()
