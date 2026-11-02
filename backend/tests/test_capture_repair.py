"""One-shot repair authorization with real durable jobs and no device calls."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.core import ArtifactStore, PlatformError, atomic_write_json
from backend.agent_runtime import AgentRunStore, AgentRuntime, CredentialLeaseStore
from backend.parallel_workbench import ParallelWorkbench, TERMINAL, create_parallel_router
from backend.runtime_action_router import PlatformRuntimeActionRouter


class RepairGateway:
    def list_bridges(self, *, project_id):
        return [{"bridge_id": "bridge_fixture", "connection_state": "connected"}]


class CaptureRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ArtifactStore(self.root, project_id="repair-test-project")
        self.gateway = RepairGateway()
        self.service = ParallelWorkbench(self.root, self.store, self.gateway)
        self.addCleanup(self.service.shutdown)
        environment = patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "device"})
        environment.start()
        self.addCleanup(environment.stop)
        app = FastAPI()

        @app.exception_handler(PlatformError)
        async def platform_error(request: Request, exc: PlatformError):
            return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status_code)

        app.include_router(create_parallel_router(self.service))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.plan = self.make_plan()
        self.payload = {"connection_mode": "bridge", "bridge_id": "bridge_fixture", "device_serial": "fixture_phone"}

    def make_plan(self, **changes):
        payload = {"template_id": "custom", "title": "Repair test fixture", "steps": [
            {"title": "prepare", "operation": "wait", "seconds": 0},
            {"title": "measure", "operation": "wait", "seconds": 0}],
            "capture": {"selected_step_ids": ["step_02"], "collectors": ["trace", "pmu"]}}
        payload.update(changes)
        return self.service.capture.create_plan(payload)

    def wait_job(self, receipt, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.service._lock:
                job = self.service.get_job(receipt["job_id"])
                if job["status"] in TERMINAL and job["job_id"] not in self.service._cancel:
                    self.assertEqual(job["status"], expected, job)
                    return job
            time.sleep(.005)
        self.fail("Fixture job did not finish")

    def source(self, result=None, *, kind="scenario_capture"):
        result = result or {"status": "failed", "error": {"code": "FIXTURE_FAILURE", "message": "synthetic capture failure"}}
        if kind != "scenario_capture":
            return self.wait_job(self.service.submit(kind, lambda **_: deepcopy(result), inputs={}, run_id="capture_" + "e" * 24), result["status"])
        with patch.object(self.service.capture, "execute", side_effect=lambda _, **kw: {**deepcopy(result), "run_id": kw["run_id"]}) as execute:
            receipt = self.service.execute_capture(self.plan["plan_id"], self.payload)
            job = self.wait_job(receipt, result["status"])
        execute.assert_called_once()
        return job

    def ticket(self):
        job = self.source()
        return job, self.service.repairs.create(job["run_id"], job["job_id"])

    def assert_code(self, code, call):
        with self.assertRaises(PlatformError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def repair_agent(self, ticket):
        connector = Mock(side_effect=AssertionError("No source or device connector is used in repair tests"))
        router = PlatformRuntimeActionRouter(artifact_store=self.store, build_worker=object(), device_gateway=self.gateway,
            default_source_connector=connector, bridge_artifact_url=lambda *_: "unused", parallel_workbench=self.service)
        runtime = AgentRuntime(store=AgentRunStore(self.root / "agent-runs"), leases=CredentialLeaseStore(),
            model_gateway=Mock(side_effect=AssertionError("No model network request is permitted")),
            action_router=router, system_prompt=lambda _: "fixture")
        run = runtime.create(project_id=self.store.project_id, goal="Repair synthetic capture", initial_input="fixture",
            mode="page_agent", context={"focused_task": {"request_context": {"repair_request_id": ticket["repair_request_id"]}}},
            budget={}, action_catalog_version="fixture", skill_versions=[])
        return router, runtime, runtime.store.read(run["run_id"])

    def test_http_create_is_idempotent_and_binds_failed_job_run_and_intent(self):
        job = self.source()
        path = f'/v1/scenario-capture/runs/{job["run_id"]}/repair-request'
        first = self.client.post(path, json={"job_id": job["job_id"]})
        again = self.client.post(path, json={"job_id": job["job_id"]})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), again.json())
        ticket = first.json()
        self.assertEqual(ticket["source_job_id"], job["job_id"])
        self.assertEqual(ticket["source_run_id"], job["run_id"])
        self.assertEqual(ticket["intent"], {"bundle": self.plan["profile"]["bundle"], "selected_step_ids": ["step_02"], "collectors": ["trace", "pmu"]})
        self.assertEqual(ticket["device_serial"], "fixture_phone")
        self.assertEqual(len(list(self.service.repairs.root.glob("*.json"))), 1)
        wrong = self.client.post('/v1/scenario-capture/runs/capture_' + 'a' * 24 + '/repair-request', json={"job_id": job["job_id"]})
        self.assertEqual(wrong.status_code, 409)
        self.assertEqual(wrong.json()["error"]["code"], "CAPTURE_REPAIR_NOT_APPLICABLE")
        other = self.source(kind="parallel_search")
        self.assert_code("CAPTURE_REPAIR_NOT_APPLICABLE", lambda: self.service.repairs.create(other["run_id"], other["job_id"]))

    def test_only_pmu_gate_or_pmu_failure_makes_partial_repairable(self):
        cases = [
            ({"pmu_preparation": {"allow_execution": False, "status": "probe_failed"}}, True),
            ({"pmu_preparation": {"status": "bundle_missing"}}, True),
            ({"steps": [{"captured": True, "collector_status": {"pmu": "missing"}}]}, True),
            ({"steps": [{"captured": True, "pmu_validation": {"sampler_preparation": {"allow_execution": False}}}]}, True),
            ({"steps": [{"captured": True, "collector_status": {"pmu": "partial", "trace": "complete"}}]}, False),
            ({"steps": [{"captured": False, "collector_status": {"pmu": "missing"}}]}, False),
            ({"quality": {"warnings": ["synthetic incomplete hilog"]}}, False),
        ]
        for manifest, allowed in cases:
            with self.subTest(manifest=manifest):
                job = self.source({"status": "partial", "manifest": manifest})
                if allowed:
                    self.assertEqual(self.service.repairs.create(job["run_id"], job["job_id"])["status"], "pending")
                else:
                    self.assert_code("CAPTURE_REPAIR_NOT_APPLICABLE", lambda: self.service.repairs.create(job["run_id"], job["job_id"]))
        job = self.source({"status": "cancelled"})
        self.assert_code("CAPTURE_REPAIR_NOT_APPLICABLE", lambda: self.service.repairs.create(job["run_id"], job["job_id"]))

    def test_pending_cancellation_and_expiry_never_start_a_retry(self):
        job, ticket = self.ticket()
        identity = ticket["repair_request_id"]
        response = self.client.post(f"/v1/scenario-capture/repair-requests/{identity}/cancel")
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(self.service.repairs.create(job["run_id"], job["job_id"])["status"], "cancelled")
        _, expired = self.ticket()
        expired["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        atomic_write_json(self.service.repairs._path(expired["repair_request_id"]), expired)
        with patch.object(self.service.capture, "execute") as execute:
            for request_id in (identity, expired["repair_request_id"]):
                self.assert_code("CAPTURE_REPAIR_DISABLED", lambda: self.service.execute_capture(self.plan["plan_id"], {**self.payload, "repair_request_id": request_id}))
            execute.assert_not_called()
        self.assertEqual(self.service.repairs.get(expired["repair_request_id"])["status"], "expired")
        self.assertEqual(len(list(self.service.root.glob("*.json"))), 2)

    def test_scope_device_and_project_protection_preserve_pending_ticket(self):
        _, ticket = self.ticket()
        identity = ticket["repair_request_id"]
        with patch.object(self.service.capture, "execute") as execute:
            for field, value in (("device_serial", "other_phone"), ("bridge_id", "other_bridge"), ("connection_mode", "local")):
                with self.subTest(field=field):
                    self.assert_code("CAPTURE_REPAIR_DEVICE_CHANGED", lambda: self.service.execute_capture(self.plan["plan_id"], {**self.payload, field: value, "repair_request_id": identity}))
            different_plans = [
                self.make_plan(profile={"bundle": "com.example.other", "ability": "MainAbility"}),
                self.make_plan(capture={"selected_step_ids": ["step_01"], "collectors": ["trace", "pmu"]}),
                self.make_plan(capture={"selected_step_ids": ["step_02"], "collectors": ["trace"]}),
            ]
            for plan in different_plans:
                self.assert_code("CAPTURE_REPAIR_SCOPE_CHANGED", lambda: self.service.execute_capture(plan["plan_id"], {**self.payload, "repair_request_id": identity}))
            execute.assert_not_called()
        self.assertEqual(self.service.repairs.get(identity)["status"], "pending")
        other_project = ParallelWorkbench(self.root, ArtifactStore(self.root, project_id="other-project"), self.gateway)
        self.addCleanup(other_project.shutdown)
        self.assert_code("CAPTURE_REPAIR_NOT_FOUND", lambda: other_project.repairs.get(identity))
        self.assert_code("CAPTURE_REPAIR_NOT_FOUND", lambda: other_project.repairs.cancel(identity))
        self.assert_code("CAPTURE_REPAIR_NOT_FOUND", lambda: other_project.repairs.execute(identity, self.plan["plan_id"], self.payload))

    def test_concurrent_retry_consumption_submits_one_job_and_replays_same_receipt(self):
        source, ticket = self.ticket()
        identity = ticket["repair_request_id"]
        repaired = self.make_plan(title="Adjusted preparation, same capture intent")
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def capture(_, **options):
            started.set()
            release.wait(3)
            return {"status": "completed", "run_id": options["run_id"]}

        payload = {**self.payload, "repair_request_id": identity}
        with patch.object(self.service.capture, "execute", side_effect=capture) as execute:
            with ThreadPoolExecutor(max_workers=12) as pool:
                receipts = list(pool.map(lambda _: self.service.execute_capture(repaired["plan_id"], payload, agent_run_id="fixture_agent"), range(12)))
            self.assertTrue(started.wait(2))
            self.assertEqual(len({receipt["job_id"] for receipt in receipts}), 1)
            self.assertEqual(len({receipt["run_id"] for receipt in receipts}), 1)
            self.assertNotEqual(receipts[0]["run_id"], source["run_id"])
            self.assertEqual(receipts[0]["inputs"]["retry_of_run_id"], source["run_id"])
            self.assertEqual(receipts[0]["inputs"]["repair_request_id"], identity)
            self.assertEqual(execute.call_count, 1)
            self.assert_code("CAPTURE_REPAIR_ALREADY_USED", lambda: self.service.execute_capture(self.plan["plan_id"], payload))
            release.set()
            done = self.wait_job(receipts[0], "succeeded")
            replay = self.service.execute_capture(repaired["plan_id"], payload)
            self.assertEqual(replay["job_id"], done["job_id"])
            self.assertEqual(execute.call_count, 1)
        self.assertEqual(self.service.repairs.get(identity)["status"], "succeeded")
        self.assertEqual(len(list(self.service.root.glob("*.json"))), 2)

    def test_failed_retry_cannot_create_a_second_repair_or_be_resubmitted(self):
        _, ticket = self.ticket()
        payload = {**self.payload, "repair_request_id": ticket["repair_request_id"]}
        with patch.object(self.service.capture, "execute", return_value={"status": "failed"}) as execute:
            retry = self.wait_job(self.service.execute_capture(self.plan["plan_id"], payload), "failed")
            self.assert_code("CAPTURE_REPAIR_LIMIT_REACHED", lambda: self.service.repairs.create(retry["run_id"], retry["job_id"]))
            repeated = self.service.execute_capture(self.plan["plan_id"], payload)
            self.assertEqual(repeated["job_id"], retry["job_id"])
            execute.assert_called_once()
        self.assertEqual(self.service.repairs.get(ticket["repair_request_id"])["status"], "failed")

    def test_submission_error_consumes_ticket_without_background_recapture(self):
        _, ticket = self.ticket()
        with patch.object(self.service, "_execute_capture", side_effect=PlatformError("SYNTHETIC_SUBMIT_FAILURE", "fixture")) as submit:
            self.assert_code("SYNTHETIC_SUBMIT_FAILURE", lambda: self.service.repairs.execute(ticket["repair_request_id"], self.plan["plan_id"], self.payload))
            self.assert_code("CAPTURE_REPAIR_ALREADY_USED", lambda: self.service.repairs.execute(ticket["repair_request_id"], self.plan["plan_id"], self.payload))
            submit.assert_called_once()
        self.assertEqual(len(list(self.service.root.glob("*.json"))), 1)

    def test_service_restart_revokes_pending_and_interrupted_submission_tickets(self):
        _, pending = self.ticket()
        _, submitting = self.ticket()
        submitting.update(status="submitting", retry_plan_id=self.plan["plan_id"])
        atomic_write_json(self.service.repairs._path(submitting["repair_request_id"]), submitting)
        restarted = ParallelWorkbench(self.root, self.store, self.gateway)
        self.addCleanup(restarted.shutdown)
        restarted.recover_after_restart()
        with patch.object(restarted.capture, "execute") as execute:
            for ticket in (pending, submitting):
                self.assertEqual(restarted.repairs.get(ticket["repair_request_id"])["status"], "cancelled")
                self.assert_code("CAPTURE_REPAIR_DISABLED", lambda: restarted.execute_capture(self.plan["plan_id"], {**self.payload, "repair_request_id": ticket["repair_request_id"]}))
            execute.assert_not_called()

    def test_cancel_submitted_ticket_cancels_child_job_and_releases_device(self):
        _, ticket = self.ticket()
        started, cancelled = threading.Event(), threading.Event()

        def capture(_, *, cancel_event, **options):
            started.set()
            if cancel_event.wait(3): cancelled.set()
            return {"status": "cancelled", "run_id": options["run_id"]}

        with patch.object(self.service.capture, "execute", side_effect=capture) as execute:
            receipt = self.service.execute_capture(self.plan["plan_id"], {**self.payload, "repair_request_id": ticket["repair_request_id"]})
            self.assertTrue(started.wait(2))
            self.assertEqual(self.service.repairs.cancel(ticket["repair_request_id"])["status"], "cancelled")
            job = self.wait_job(receipt, "cancelled")
            self.assertTrue(job["cancel_requested"])
            self.assertTrue(cancelled.is_set())
            self.assertEqual(self.service._device_jobs, {})
            self.assert_code("CAPTURE_REPAIR_DISABLED", lambda: self.service.execute_capture(self.plan["plan_id"], {**self.payload, "repair_request_id": ticket["repair_request_id"]}))
            execute.assert_called_once()
        self.assertEqual(self.service.repairs.get(ticket["repair_request_id"])["status"], "cancelled")

    def test_runtime_injects_omitted_ticket_and_rechecks_cancellation_instead_of_reusing_checkpoint(self):
        _, ticket = self.ticket()
        router, runtime, run = self.repair_agent(ticket)
        # The Agent supplies only the plan. Ticket context must supply the
        # original device and remain effective after the first tool success.
        inputs = {"plan_id": self.plan["plan_id"]}
        self.assertFalse(router.can_reuse("scenario_capture.execute", inputs, {}, run))
        started = threading.Event()

        def capture(_, *, cancel_event, **options):
            started.set()
            cancel_event.wait(3)
            return {"status": "cancelled", "run_id": options["run_id"]}

        with patch.object(self.service.capture, "execute", side_effect=capture) as execute:
            first = runtime.execute_external_action(run["run_id"], "scenario_capture.execute", inputs)
            self.assertEqual(first["status"], "succeeded", first)
            self.assertTrue(started.wait(2))
            job = first["result"]
            self.assertEqual(job["inputs"]["repair_request_id"], ticket["repair_request_id"])
            self.assertEqual(job["inputs"]["device_serial"], "fixture_phone")
            self.service.repairs.cancel(ticket["repair_request_id"])
            self.wait_job(job, "cancelled")
            second = runtime.execute_external_action(run["run_id"], "scenario_capture.execute", inputs)
            self.assertEqual(second["status"], "failed", second)
            self.assertEqual(second["error"]["code"], "CAPTURE_REPAIR_DISABLED")
            self.assertNotIn("reused_from_checkpoint", second)
            execute.assert_called_once()
        self.assertNotIn("repair_request_id", inputs, "Router must not mutate the caller's action input")

    def test_runtime_rejects_explicit_different_ticket_and_disables_reuse_for_explicit_ticket(self):
        _, ticket = self.ticket()
        router, runtime, run = self.repair_agent(ticket)
        inputs = {"plan_id": self.plan["plan_id"], "repair_request_id": "capture_repair_" + "b" * 24}
        with patch.object(self.service.capture, "execute") as execute:
            result = runtime.execute_external_action(run["run_id"], "scenario_capture.execute", inputs)
            self.assertEqual(result["status"], "failed", result)
            self.assertEqual(result["error"]["code"], "CAPTURE_REPAIR_CONTEXT_MISMATCH")
            execute.assert_not_called()
        self.assertEqual(self.service.repairs.get(ticket["repair_request_id"])["status"], "pending")
        context_free = {**run, "context": {}}
        self.assertFalse(router.can_reuse("scenario_capture.execute", inputs, {}, context_free))
        self.assertTrue(router.can_reuse("scenario_capture.execute", {"plan_id": self.plan["plan_id"]}, {}, context_free))

    def test_lost_submitted_receipt_recovers_child_for_cancel_and_restart(self):
        for action in ("cancel", "restart"):
            with self.subTest(action=action):
                _, ticket = self.ticket()
                identity = ticket["repair_request_id"]
                started, stopped, write_failed = threading.Event(), threading.Event(), threading.Event()
                original_write = self.service.repairs._write

                def write(value):
                    if value["status"] == "submitted" and not write_failed.is_set():
                        write_failed.set()
                        raise OSError("synthetic receipt persistence failure")
                    return original_write(value)

                def capture(_, *, cancel_event, **options):
                    started.set()
                    if cancel_event.wait(3): stopped.set()
                    return {"status": "cancelled", "run_id": options["run_id"]}

                with patch.object(self.service.repairs, "_write", side_effect=write), \
                        patch.object(self.service.capture, "execute", side_effect=capture) as execute:
                    with self.assertRaisesRegex(OSError, "receipt persistence failure"):
                        self.service.execute_capture(self.plan["plan_id"], {**self.payload, "repair_request_id": identity})
                    self.assertTrue(started.wait(2))
                    stored = json.loads(self.service.repairs._path(identity).read_text())
                    self.assertEqual(stored["status"], "submitting")
                    self.assertIsNone(stored["retry_job_id"])
                    if action == "cancel":
                        recovered = self.service.repairs.cancel(identity)
                    else:
                        self.service.repairs.recover_after_restart()
                        recovered = self.service.repairs.get(identity)
                    self.assertEqual(recovered["status"], "cancelled")
                    self.assertTrue(recovered["child_receipt_recovered"])
                    child = self.wait_job({"job_id": recovered["retry_job_id"]}, "cancelled")
                    self.assertTrue(child["cancel_requested"])
                    self.assertEqual(child["inputs"]["repair_request_id"], identity)
                    self.assertTrue(stopped.is_set())
                    execute.assert_called_once()
                    self.assert_code("CAPTURE_REPAIR_DISABLED", lambda: self.service.execute_capture(self.plan["plan_id"], {**self.payload, "repair_request_id": identity}))


if __name__ == "__main__":
    unittest.main()
