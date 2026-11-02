"""Automatic discovery and target pinning using fake device gateways only."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.core import ArtifactStore, PlatformError
from backend.parallel_workbench import ParallelWorkbench, TERMINAL, create_parallel_router
from backend.tests.test_scenario_capture import SimulatedDeviceClient


class DiscoveryGateway:
    def __init__(self):
        self.local_devices = []
        self.local_error = None
        self.local_calls = []
        self.bridges = []
        self.results = {}
        self.jobs = []
        self.waits = []
        self.projects = []
        self.delay = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def add_bridge(self, bridge_id, devices=(), *, online=True, result=None):
        self.bridges.append({"bridge_id": bridge_id, "display_name": bridge_id,
            "connection_state": "connected" if online else "offline",
            "capabilities": {"runtime_diagnostics": {"hdc": {"stdout": "stale-phone"}}}})
        self.results[bridge_id] = result if result is not None else {
            "status": "succeeded", "devices": [{"device_serial": serial} for serial in devices]}

    def list_bridges(self, *, project_id):
        self.projects.append(project_id)
        return self.bridges

    def local_discover(self, **kwargs):
        self.local_calls.append(kwargs)
        if self.local_error:
            raise self.local_error
        return {"hdc": {"available": True}, "devices": [{"serial": serial} for serial in self.local_devices]}

    def _bridge_job(self, **kwargs):
        with self.lock:
            job = {**kwargs, "job_id": "fake_" + str(len(self.jobs))}
            self.jobs.append(job)
            return job

    def dispatch_bridge_job(self, job, *, wait_seconds):
        with self.lock:
            self.waits.append(wait_seconds)
            self.active += 1
            self.max_active = max(self.active, self.max_active)
        try:
            if self.delay:
                time.sleep(self.delay)
            result = self.results[job["bridge_id"]]
            if isinstance(result, Exception):
                raise result
            return {"result": result}
        finally:
            with self.lock:
                self.active -= 1


class ScenarioAutoDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gateway = DiscoveryGateway()
        self.service = ParallelWorkbench(self.root, ArtifactStore(self.root, project_id="auto-project"), self.gateway)
        self.addCleanup(self.service.shutdown)
        self.binary = patch("backend.scenario_capture.resolve_hdc_binary", return_value=Path("/stub/hdc"))
        self.binary.start()
        self.addCleanup(self.binary.stop)
        self.environment = patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "device"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        app = FastAPI()

        @app.exception_handler(PlatformError)
        async def error_handler(request: Request, exc: PlatformError):
            return JSONResponse({"error": {"code": exc.code, "message": str(exc), "details": exc.details}}, status_code=exc.status_code)

        app.include_router(create_parallel_router(self.service))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def plan(self):
        return self.service.capture.create_plan({"template_id": "custom", "steps": [
            {"title": "prepare", "operation": "wait", "seconds": 0},
            {"title": "capture", "operation": "wait", "seconds": 0}],
            "capture": {"selected_step_ids": ["step_02"], "collectors": ["trace"]}})

    def wait_job(self, job):
        for _ in range(200):
            value = self.service.get_job(job["job_id"])
            if value["status"] in TERMINAL:
                return value
            time.sleep(.005)
        self.fail("Fake capture job did not finish")

    def test_catalog_auto_default_is_platform_independent(self):
        for platform in ("darwin", "win32", "linux"):
            with patch("backend.scenario_capture.sys.platform", platform):
                catalog = self.service.capture.catalog()
                self.assertEqual(catalog["default_connection_mode"], "auto")
                self.assertEqual({item["id"] for item in catalog["connection_modes"]}, {"auto", "local", "bridge"})

    def test_all_routes_are_discovered_even_when_local_phone_is_connected(self):
        self.gateway.local_devices = ["local-phone"]
        self.gateway.add_bridge("remote", ["remote-phone"])
        result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["status"], "selection_required")
        self.assertIsNone(result["selected_device"])
        self.assertTrue(result["discovery_complete"])
        self.assertEqual(result["devices"][0], {"serial": "local-phone", "device_serial": "local-phone",
            "connection_mode": "local", "bridge_id": "", "display_name": "local-phone", "connection_label": "本机 HDC 直连"})
        self.assertEqual(result["connection_mode"], "auto")
        self.assertTrue(result["observed_at"])
        self.assertEqual(self.gateway.local_calls, [{"timeout_seconds": 5}])
        self.assertEqual(result["devices"][1]["bridge_id"], "remote")
        self.assertEqual(len(self.gateway.jobs), 1)
        self.assertEqual(self.gateway.projects, ["auto-project"])

    def test_empty_local_discovers_online_bridge_fresh_without_stale_capabilities(self):
        self.gateway.add_bridge("remote", ["fresh-phone"])
        self.gateway.add_bridge("offline", ["old-phone"], online=False)
        result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["selected_device"]["device_serial"], "fresh-phone")
        self.assertEqual(result["selected_device"]["bridge_id"], "remote")
        self.assertEqual(result["selected_device"]["connection_mode"], "bridge")
        self.assertEqual(self.gateway.projects, ["auto-project"])
        self.assertEqual(len(self.gateway.jobs), 1)
        self.assertEqual(self.gateway.jobs[0]["action"], "device.discover")
        self.assertEqual(self.gateway.jobs[0]["payload"], {"targets_only": True})
        self.assertEqual(self.gateway.jobs[0]["timeout_seconds"], 10)
        self.assertEqual(self.gateway.waits, [10])
        self.assertNotIn("stale-phone", str(result))

    def test_explicit_bridge_discovery_is_lightweight_and_route_normalized(self):
        self.gateway.add_bridge("remote", ["phone"])
        result = self.service.discover_devices("remote", connection_mode="bridge")
        self.assertEqual(result["connection_mode"], "bridge")
        self.assertEqual(self.gateway.jobs[0]["payload"], {"targets_only": True})
        self.assertEqual(self.gateway.waits, [10])
        self.assertTrue(result["discovery_complete"])
        self.assertEqual(result["devices"][0]["connection_mode"], "bridge")
        self.assertEqual(result["devices"][0]["bridge_id"], "remote")
        self.assertFalse(self.gateway.local_calls)
        self.assertNotIn("stale-phone", str(result))

    def test_local_failure_falls_back_to_bridge(self):
        self.gateway.local_error = PlatformError("DEVICE_COMMAND_TIMEOUT", "timeout", status_code=504)
        self.gateway.add_bridge("remote", ["remote-phone"])
        result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["status"], "selection_required")
        self.assertFalse(result["discovery_complete"])
        self.assertIsNone(result["selected_device"])
        self.assertEqual(result["diagnostics"][0]["code"], "DEVICE_COMMAND_TIMEOUT")
        self.assertEqual(result["devices"][0]["connection_mode"], "bridge")

    def test_missing_local_hdc_falls_back_without_local_invocation(self):
        self.gateway.add_bridge("remote", ["phone"])
        with patch.object(self.service.capture, "catalog", return_value={"local_hdc": {"available": False}}):
            result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["status"], "ready")
        self.assertFalse(self.gateway.local_calls)

    def test_multiple_local_devices_require_selection(self):
        self.gateway.local_devices = ["first", "second"]
        result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["status"], "selection_required")
        self.assertIsNone(result["selected_device"])
        self.assertEqual(len(result["devices"]), 2)

    def test_bridge_concurrency_is_bounded_at_four(self):
        for index in range(8):
            self.gateway.add_bridge("bridge_" + str(index), ["phone_" + str(index)])
        self.gateway.delay = .025
        result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["status"], "selection_required")
        self.assertEqual(len(result["devices"]), 8)
        self.assertGreater(self.gateway.max_active, 1)
        self.assertLessEqual(self.gateway.max_active, 4)
        self.assertEqual(self.gateway.active, 0)

    def test_failed_bridge_never_causes_incomplete_automatic_selection(self):
        self.gateway.add_bridge("online", ["phone"])
        self.gateway.add_bridge("silent", result=PlatformError("BRIDGE_JOB_TIMEOUT", "timeout", status_code=504))
        result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["status"], "selection_required")
        self.assertFalse(result["discovery_complete"])
        self.assertIsNone(result["selected_device"])
        self.assertEqual(len(result["devices"]), 1)
        with self.assertRaises(PlatformError) as caught:
            self.service.execute_capture(self.plan()["plan_id"], {"connection_mode": "auto"})
        self.assertEqual(caught.exception.code, "SCENARIO_DEVICE_DISCOVERY_INCOMPLETE")

    def test_batch_budget_skips_queued_probes_and_waits_for_workers(self):
        for index in range(8):
            self.gateway.add_bridge("bridge_" + str(index), ["phone_" + str(index)])
        self.gateway.delay = 1.05
        with patch("backend.scenario_capture.AUTO_BRIDGE_DISCOVERY_SECONDS", 2):
            result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["status"], "selection_required")
        self.assertFalse(result["discovery_complete"])
        self.assertEqual(len(self.gateway.jobs), 4)
        self.assertEqual(self.gateway.active, 0)
        self.assertEqual(sum(item["status"] == "not_probed" for item in result["diagnostics"]), 4)

    def test_empty_or_malformed_bridge_results_do_not_trust_cached_device(self):
        self.gateway.add_bridge("empty")
        result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["status"], "unavailable")
        self.gateway.results["empty"] = {"status": "succeeded"}
        result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["status"], "discovery_incomplete")
        self.assertEqual(result["devices"], [])

    def test_auto_execute_without_serial_persists_concrete_mode_and_auto_origin(self):
        self.gateway.local_devices = ["phone"]
        original = self.service.capture.execute
        def fake_execute(*args, **kwargs):
            return original(*args, **kwargs, device_client=SimulatedDeviceClient())
        with patch.object(self.service.capture, "execute", side_effect=fake_execute):
            job = self.wait_job(self.service.execute_capture(self.plan()["plan_id"], {"connection_mode": "auto"}))
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(job["inputs"]["connection_mode"], "local")
        self.assertEqual(job["inputs"]["requested_connection_mode"], "auto")
        run = job["result"]
        self.assertEqual(run["device_serial"], "phone")
        self.assertEqual(run["connection_mode"], "local")
        self.assertEqual(run["requested_connection_mode"], "auto")
        self.assertEqual(run["manifest"]["execution_provenance"]["requested_connection_mode"], "auto")

    def test_auto_execute_multiple_devices_requires_selection_and_never_queues(self):
        self.gateway.local_devices = ["one", "two"]
        with self.assertRaises(PlatformError) as caught:
            self.service.execute_capture(self.plan()["plan_id"], {"connection_mode": "auto"})
        self.assertEqual(caught.exception.code, "SCENARIO_DEVICE_SELECTION_REQUIRED")
        self.assertEqual(caught.exception.status_code, 422)
        self.assertFalse(list(self.service.root.glob("*.json")))

    def test_selected_serial_is_pinned_when_another_device_replaces_it(self):
        self.gateway.local_devices = ["replacement"]
        with self.assertRaises(PlatformError) as caught:
            self.service.execute_capture(self.plan()["plan_id"], {"connection_mode": "auto", "device_serial": "original"})
        self.assertEqual(caught.exception.code, "SCENARIO_DEVICE_NOT_CONNECTED")
        self.assertFalse(list(self.service.root.glob("*.json")))

    def test_auto_execute_selected_serial_among_multiple_devices(self):
        self.gateway.local_devices = ["one", "two"]
        with patch.object(self.service.capture, "execute", return_value={"status": "succeeded"}) as execute:
            self.wait_job(self.service.execute_capture(self.plan()["plan_id"], {"connection_mode": "auto", "device_serial": "two"}))
        self.assertEqual(execute.call_args.kwargs["device_serial"], "two")

    def test_selected_bridge_remains_pinned_even_if_local_phone_appears(self):
        self.gateway.local_devices = ["local-phone"]
        self.gateway.add_bridge("remote", ["remote-phone"])
        with patch.object(self.service.capture, "execute", return_value={"status": "succeeded"}) as execute:
            self.wait_job(self.service.execute_capture(self.plan()["plan_id"], {
                "connection_mode": "auto", "bridge_id": "remote", "device_serial": "remote-phone"}))
        self.assertEqual(execute.call_args.kwargs["connection_mode"], "bridge")
        self.assertEqual(execute.call_args.kwargs["bridge_id"], "remote")
        self.assertFalse(self.gateway.local_calls)
        self.assertEqual(self.gateway.waits, [10])
        self.assertEqual(self.gateway.jobs[0]["payload"], {"targets_only": True})

    def test_auto_discovery_with_explicit_bridge_only_probes_that_route(self):
        self.gateway.local_devices = ["local-phone"]
        self.gateway.add_bridge("selected", ["remote-phone"])
        self.gateway.add_bridge("other", result=RuntimeError("unrelated route failed"))
        result = self.service.discover_devices("selected", connection_mode="auto")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["devices"][0]["bridge_id"], "selected")
        self.assertTrue(result["discovery_complete"])
        self.assertEqual([job["bridge_id"] for job in self.gateway.jobs], ["selected"])
        self.assertFalse(self.gateway.local_calls)
        self.assertEqual(len(result["diagnostics"]), 1)

    def test_explicit_local_discovery_never_touches_other_routes(self):
        self.gateway.local_devices = ["phone"]
        self.gateway.add_bridge("other", result=RuntimeError("unrelated route failed"))
        result = self.service.discover_devices("stale-bridge", connection_mode="local")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["devices"][0]["connection_mode"], "local")
        self.assertEqual(result["devices"][0]["bridge_id"], "")
        self.assertEqual(self.gateway.local_calls, [{"timeout_seconds": 5}])
        self.assertFalse(self.gateway.jobs)
        self.assertFalse(self.gateway.projects)

    def test_partial_discovery_does_not_block_explicit_working_bridge(self):
        self.gateway.add_bridge("online", ["phone"])
        self.gateway.add_bridge("silent", result=PlatformError("BRIDGE_JOB_TIMEOUT", "timeout", status_code=504))
        result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["status"], "selection_required")
        self.assertFalse(result["discovery_complete"])
        self.assertEqual([row["device_serial"] for row in result["devices"]], ["phone"])
        self.gateway.jobs.clear()
        with patch.object(self.service.capture, "execute", return_value={"status": "succeeded"}) as execute:
            self.wait_job(self.service.execute_capture(self.plan()["plan_id"], {
                "connection_mode": "auto", "bridge_id": "online", "device_serial": "phone"}))
        self.assertEqual(execute.call_args.kwargs["bridge_id"], "online")
        self.assertEqual([job["bridge_id"] for job in self.gateway.jobs], ["online"])

    def test_serial_without_route_stays_ambiguous_when_another_bridge_is_unconfirmed(self):
        self.gateway.add_bridge("online", ["phone"])
        self.gateway.add_bridge("silent", result=PlatformError("BRIDGE_JOB_TIMEOUT", "timeout", status_code=504))
        with self.assertRaises(PlatformError) as caught:
            self.service.execute_capture(self.plan()["plan_id"], {"connection_mode": "auto", "device_serial": "phone"})
        self.assertEqual(caught.exception.code, "SCENARIO_DEVICE_DISCOVERY_INCOMPLETE")
        self.assertFalse(list(self.service.root.glob("*.json")))

    def test_same_serial_on_local_and_bridge_requires_route_selection(self):
        self.gateway.local_devices = ["same"]
        self.gateway.add_bridge("remote", ["same"])
        result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(len(result["devices"]), 2)
        self.assertEqual({item["connection_mode"] for item in result["devices"]}, {"local", "bridge"})
        with self.assertRaises(PlatformError) as caught:
            self.service.execute_capture(self.plan()["plan_id"], {"connection_mode": "auto", "device_serial": "same"})
        self.assertEqual(caught.exception.code, "SCENARIO_DEVICE_SELECTION_REQUIRED")

    def test_route_failure_empty_and_offline_have_distinct_diagnostics(self):
        variants = [
            ({"status": "failed", "devices": [{"serial": "stale-phone"}]}, "failed", False, "discovery_incomplete"),
            ({"status": "succeeded", "devices": []}, "empty", True, "unavailable"),
            (PlatformError("BRIDGE_JOB_TIMEOUT", "timeout", status_code=504), "timed_out", False, "discovery_incomplete"),
            (PlatformError("BRIDGE_OFFLINE", "offline", status_code=503), "offline", True, "unavailable"),
        ]
        self.gateway.add_bridge("selected")
        for value, diagnostic_status, complete, status in variants:
            with self.subTest(status=diagnostic_status):
                self.gateway.results["selected"] = value
                result = self.service.discover_devices("selected", connection_mode="bridge")
                self.assertEqual(result["devices"], [])
                self.assertEqual(result["status"], status)
                self.assertEqual(result["discovery_complete"], complete)
                self.assertEqual(result["diagnostics"][0]["status"], diagnostic_status)
                self.assertEqual(result["diagnostics"][0]["bridge_id"], "selected")

    def test_bridge_registry_failure_retains_confirmed_local_device_for_explicit_selection(self):
        self.gateway.local_devices = ["phone"]
        with patch.object(self.gateway, "list_bridges", side_effect=RuntimeError("registry unavailable")):
            result = self.service.discover_devices(connection_mode="auto")
        self.assertEqual(result["status"], "selection_required")
        self.assertFalse(result["discovery_complete"])
        self.assertIsNone(result["selected_device"])
        self.assertEqual(result["devices"][0]["device_serial"], "phone")

    def test_missing_local_hdc_returns_route_diagnostic_without_global_http_error(self):
        with patch.object(self.gateway, "local_discover", return_value={
                "hdc": {"available": False, "error_code": "DEVICE_HDC_UNAVAILABLE", "diagnostic": "missing"}, "devices": []}):
            response = self.client.post('/v1/scenario-capture/devices/discover', json={"connection_mode": "local"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["diagnostics"][0]["code"], "DEVICE_HDC_UNAVAILABLE")
        self.assertEqual(result["diagnostics"][0]["status"], "unavailable")
        self.assertFalse(self.gateway.jobs)

    def test_real_targets_only_bridge_envelope_uses_fresh_devices_despite_stale_health(self):
        self.gateway.add_bridge("windows", result={
            "status": "succeeded", "devices": [{"device_serial": "PHONE", "connection_state": "connected"}],
            "hdc": {"return_code": 0, "stdout": "PHONE\r\n", "stderr": "", "timed_out": False, "cancelled": False},
            "discovery_profile": "targets_only", "bridge_diagnostics_source": "cached",
            "bridge_diagnostics": {"status": "attention", "hdc": {"target_count": 0}},
        })
        result = self.service.discover_devices("windows", connection_mode="bridge")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["selected_device"]["device_serial"], "PHONE")
        self.assertEqual(result["selected_device"]["bridge_id"], "windows")
        self.assertEqual(self.gateway.jobs[0]["payload"], {"targets_only": True})

    def test_bridge_discovery_command_deadline_keeps_cause_instead_of_reporting_empty_phone_list(self):
        self.gateway.add_bridge("windows", result={
            "status": "timed_out", "devices": [], "hdc": {"return_code": None, "timed_out": True,
                "error_code": "BRIDGE_DISCOVERY_DEADLINE", "stderr": "waiting for HDC lock", "duration_ms": 9999},
        })
        result = self.service.discover_devices("windows", connection_mode="bridge")
        diagnostic = result["diagnostics"][0]
        self.assertEqual(result["status"], "discovery_incomplete")
        self.assertEqual(diagnostic["status"], "timed_out")
        self.assertEqual(diagnostic["code"], "BRIDGE_DISCOVERY_DEADLINE")
        self.assertIn("waiting for HDC lock", diagnostic["message"])
        self.assertEqual(diagnostic["hdc"]["duration_ms"], 9999)

    def test_bridge_process_start_failure_preserves_nested_command_error(self):
        self.gateway.add_bridge("windows", result={
            "status": "failed", "devices": [], "hdc": {"return_code": None,
                "launch_error": {"code": "BRIDGE_HDC_PROCESS_START_FAILED", "message": "hdc.exe access denied"}},
        })
        result = self.service.discover_devices("windows", connection_mode="bridge")
        self.assertFalse(result["discovery_complete"])
        self.assertEqual(result["diagnostics"][0]["code"], "BRIDGE_HDC_PROCESS_START_FAILED")
        self.assertIn("hdc.exe access denied", result["diagnostics"][0]["message"])

    def test_failed_hdc_command_and_transport_deadline_never_claim_phone_disconnection(self):
        self.gateway.add_bridge("windows")
        for command in ({"return_code": 1, "stderr": "daemon unavailable"}, {"return_code": None, "timed_out": True}):
            with self.subTest(command=command):
                self.gateway.results["windows"] = {"status": "succeeded", "devices": [{"device_serial": "stale"}], "hdc": command}
                result = self.service.discover_devices("windows", connection_mode="bridge")
                self.assertEqual(result["devices"], [])
                self.assertFalse(result["discovery_complete"])
        self.gateway.results["windows"] = PlatformError("bridge_job_deadline", "deadline", status_code=504)
        result = self.service.discover_devices("windows", connection_mode="bridge")
        self.assertEqual(result["diagnostics"][0]["status"], "timed_out")

    def test_identical_serials_on_two_bridges_need_bridge_selection(self):
        self.gateway.add_bridge("first", ["same"])
        self.gateway.add_bridge("second", ["same"])
        with self.assertRaises(PlatformError) as caught:
            self.service.execute_capture(self.plan()["plan_id"], {"connection_mode": "auto", "device_serial": "same"})
        self.assertEqual(caught.exception.code, "SCENARIO_DEVICE_SELECTION_REQUIRED")

    def test_failed_selected_bridge_is_not_reported_as_device_disconnection(self):
        self.gateway.add_bridge("remote", result={"status": "failed", "devices": []})
        with self.assertRaises(PlatformError) as caught:
            self.service.execute_capture(self.plan()["plan_id"], {
                "connection_mode": "auto", "bridge_id": "remote", "device_serial": "phone"})
        self.assertEqual(caught.exception.code, "SCENARIO_DEVICE_DISCOVERY_INCOMPLETE")
        self.assertFalse(list(self.service.root.glob("*.json")))

    def test_http_contract_accepts_auto_and_execute_with_plan_only(self):
        self.gateway.local_devices = ["phone"]
        response = self.client.post("/v1/scenario-capture/devices/discover", json={"connection_mode": "auto"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue({"schema_version", "connection_mode", "devices", "selected_device", "status", "message", "diagnostics", "observed_at"} <= response.json().keys())
        plan = self.plan()
        with patch.object(self.service.capture, "execute", return_value={"status": "succeeded"}):
            response = self.client.post(f'/v1/scenario-capture/plans/{plan["plan_id"]}/execute', json={"connection_mode": "auto"})
            self.assertEqual(response.status_code, 200, response.text)
            self.wait_job(response.json())

    def test_missing_mode_retains_legacy_bridge_requirement(self):
        plan = self.plan()
        response = self.client.post(f'/v1/scenario-capture/plans/{plan["plan_id"]}/execute', json={})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "SCENARIO_DEVICE_REQUIRED")
        self.assertFalse(self.gateway.local_calls)


if __name__ == "__main__":
    unittest.main()
