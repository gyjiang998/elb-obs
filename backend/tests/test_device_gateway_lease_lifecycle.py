"""Lease lifecycle and transport races; all Bridges are in-process fakes."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend.core import PlatformError
from backend.device_gateway import DeviceGateway, PendingJob


class DeviceGatewayLeaseLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.gateway = DeviceGateway(workspace_root=root, state_root=root / "state")
        identity = self.gateway.register_trusted_bridge(project_id="test", display_name="fake", capabilities={})
        self.bridge_id = identity["bridge_id"]
        self.secret = identity["bridge_secret"]
        self.target = "device_test"
        self.sent: list[dict] = []
        self.connect()
        self.lease = self.create_lease()

    def connect(self, send=None) -> None:
        self.gateway.connect_bridge(self.bridge_id, self.secret, capabilities={}, send=send or self.sent.append)

    def create_lease(self, **overrides):
        return self.gateway.create_lease(**{
            "project_id": "test", "bridge_id": self.bridge_id, "logical_device_id": self.target, **overrides,
        })

    def job(self, *, key="command", lease_id=None, target=None):
        return self.gateway._bridge_job(project_id="test", agent_run_id="run_test", bridge_id=self.bridge_id,
            lease_id=lease_id or self.lease["lease_id"], logical_device_id=target or self.target,
            action="device.execute_shell", payload={"command": "fake"}, idempotency_key=key, timeout_seconds=10)

    def success(self, job):
        self.gateway.report_bridge_result(self.bridge_id, {"job_id": job["job_id"], "result": {"status": "succeeded"}})

    def expire(self, lease_id):
        with self.gateway._lock:
            registry = self.gateway._read_registry()
            registry["leases"][lease_id]["expires_at_epoch"] = time.time() - 1
            self.gateway._write_registry(registry)

    def legacy_offline_job(self):
        job = self.job(key="legacy-offline")
        self.gateway._record_issued_job(job)
        self.gateway._record_job_state(job["job_id"], "bridge_offline")
        with self.gateway._lock:
            registry = self.gateway._read_registry()
            registry["leases"][self.lease["lease_id"]]["active_job_id"] = job["job_id"]
            self.gateway._write_registry(registry)
        return job

    def test_heartbeat_cannot_overwrite_a_concurrently_created_lease(self):
        paused, release, creating = threading.Event(), threading.Event(), threading.Event()
        original = self.gateway._write_registry
        errors = []
        created = []

        def write(registry):
            if threading.current_thread().name == "heartbeat":
                paused.set()
                if not release.wait(3):
                    raise AssertionError("test heartbeat was not released")
            original(registry)

        def create():
            creating.set()
            try:
                created.append(self.create_lease(logical_device_id="device_second"))
            except Exception as exc:
                errors.append(exc)

        with patch.object(self.gateway, "_write_registry", side_effect=write):
            heartbeat = threading.Thread(target=lambda: self.gateway.heartbeat(self.bridge_id), name="heartbeat")
            heartbeat.start()
            try:
                self.assertTrue(paused.wait(2))
                creator = threading.Thread(target=create)
                creator.start()
                self.assertTrue(creating.wait(2))
                # In the old implementation create finished here and the stale
                # heartbeat write then erased its successful lease response.
                creator.join(.1)
            finally:
                release.set()
            heartbeat.join(2)
            creator.join(2)
        self.assertFalse(errors)
        self.assertFalse(creator.is_alive())
        self.assertIn(created[0]["lease_id"], self.gateway._read_registry()["leases"])

    def test_create_reuses_and_renews_one_lease_for_the_same_target(self):
        with self.gateway._lock:
            registry = self.gateway._read_registry()
            registry["leases"][self.lease["lease_id"]]["expires_at_epoch"] = time.time() + 5
            self.gateway._write_registry(registry)
        results = []
        workers = [threading.Thread(target=lambda: results.append(self.create_lease())) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        self.assertEqual({item["lease_id"] for item in results}, {self.lease["lease_id"]})
        self.assertEqual(len(self.gateway._read_registry()["leases"]), 1)
        self.assertGreater(min(item["expires_at_epoch"] for item in results), time.time() + 300)

    def test_offline_before_send_does_not_reserve_or_consume_idempotency(self):
        job = self.job()
        self.gateway.disconnect_bridge(self.bridge_id)
        with self.assertRaises(PlatformError) as raised:
            self.gateway.dispatch_bridge_job(job, wait_seconds=1)
        self.assertEqual(raised.exception.code, "BRIDGE_OFFLINE")
        registry = self.gateway._read_registry()
        self.assertEqual(registry["jobs"], {})
        self.assertEqual(registry["idempotency_index"], {})
        self.assertIsNone(registry["leases"][self.lease["lease_id"]]["active_job_id"])
        self.connect(lambda message: self.success(message["job"]))
        self.assertEqual(self.gateway.dispatch_bridge_job(job, wait_seconds=1)["result"]["status"], "succeeded")

    def test_fast_result_and_late_acceptance_do_not_regress_terminal_state(self):
        self.connect(lambda message: self.success(message["job"]))
        job = self.job()
        result = self.gateway.dispatch_bridge_job(job, wait_seconds=1)
        self.gateway.mark_bridge_job_accepted(self.bridge_id, job["job_id"])
        self.gateway._record_job_state(job["job_id"], "state_unknown")
        self.gateway.report_bridge_result(self.bridge_id, {"job_id": job["job_id"], "result": {"status": "failed"}})
        entry = self.gateway._read_registry()["jobs"][job["job_id"]]
        self.assertEqual(result["result"]["status"], "succeeded")
        self.assertEqual(entry["state"], "succeeded")
        self.assertEqual(entry["result"]["result"]["status"], "succeeded")
        self.assertTrue(self.gateway.dispatch_bridge_job(self.job(), wait_seconds=1)["idempotency_reused"])

    def test_a_result_at_the_wait_deadline_wins_over_timeout(self):
        job = self.job()
        event = threading.Event()
        pending = PendingJob(event=event)

        def deadline_result(timeout):
            self.success(job)
            return False

        with patch("backend.device_gateway.PendingJob", return_value=pending), patch.object(event, "wait", side_effect=deadline_result):
            result = self.gateway.dispatch_bridge_job(job, wait_seconds=1)
        self.assertEqual(result["result"]["status"], "succeeded")
        self.assertEqual(self.gateway._read_registry()["jobs"][job["job_id"]]["state"], "succeeded")

    def test_transport_send_does_not_hold_registry_transaction_lock(self):
        finished = threading.Event()
        workers = []

        def send(message):
            def receive():
                self.gateway.heartbeat(self.bridge_id)
                self.success(message["job"])
                finished.set()
            worker = threading.Thread(target=receive)
            workers.append(worker)
            worker.start()
            if not finished.wait(1):
                raise AssertionError("send held the gateway transaction lock")

        self.connect(send)
        try:
            result = self.gateway.dispatch_bridge_job(self.job(), wait_seconds=1)
        finally:
            for worker in workers:
                worker.join(2)
        self.assertEqual(result["result"]["status"], "succeeded")

    def test_dispatch_excludes_concurrent_jobs_even_using_different_legacy_leases(self):
        legacy_id = "lease_" + "f" * 24
        with self.gateway._lock:
            registry = self.gateway._read_registry()
            registry["leases"][legacy_id] = {**deepcopy(self.lease), "lease_id": legacy_id}
            self.gateway._write_registry(registry)
        first = self.job(key="first")
        second = self.job(key="second", lease_id=legacy_id)
        sent = threading.Event()
        self.connect(lambda message: (self.sent.append(message), sent.set()))
        results = []
        worker = threading.Thread(target=lambda: results.append(self.gateway.dispatch_bridge_job(first, wait_seconds=2)))
        worker.start()
        try:
            self.assertTrue(sent.wait(1))
            with self.assertRaises(PlatformError) as raised:
                self.gateway.dispatch_bridge_job(second, wait_seconds=1)
            self.assertEqual(raised.exception.code, "DEVICE_LEASE_BUSY")
            self.assertEqual(len(self.sent), 1)
            self.assertNotIn(second["job_id"], self.gateway._read_registry()["jobs"])
            self.assertEqual(self.create_lease()["lease_id"], self.lease["lease_id"])
        finally:
            self.success(first)
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["result"]["status"], "succeeded")

    def test_unknown_sent_job_cannot_be_bypassed_by_expiry_or_new_lease(self):
        self.connect(lambda message: (_ for _ in ()).throw(ConnectionError("simulated send uncertainty")))
        job = self.job()
        with self.assertRaises(ConnectionError):
            self.gateway.dispatch_bridge_job(job, wait_seconds=1)
        self.expire(self.lease["lease_id"])
        with self.assertRaises(PlatformError) as raised:
            self.create_lease()
        self.assertEqual(raised.exception.code, "DEVICE_LEASE_BUSY")
        self.assertEqual(len(self.gateway._read_registry()["leases"]), 1)
        self.assertEqual(self.gateway.reconcile_bridge_jobs(self.bridge_id, []), [])

    def test_audited_legacy_pre_send_offline_failure_is_recovered_and_retryable(self):
        job = self.legacy_offline_job()
        self.assertEqual(self.gateway.reconcile_bridge_jobs(self.bridge_id, []), [])
        registry = self.gateway._read_registry()
        self.assertEqual(registry["jobs"][job["job_id"]]["state"], "failed")
        self.assertEqual(registry["jobs"][job["job_id"]]["delivery_state"], "not_sent")
        self.assertIsNone(registry["leases"][self.lease["lease_id"]]["active_job_id"])
        self.assertEqual(registry["idempotency_index"], {})
        self.connect(lambda message: self.success(message["job"]))
        self.assertEqual(self.gateway.dispatch_bridge_job(self.job(key="legacy-offline"), wait_seconds=1)["result"]["status"], "succeeded")

    def test_legacy_offline_with_bridge_receipt_is_not_recovered(self):
        job = self.legacy_offline_job()
        self.gateway.reconcile_bridge_jobs(self.bridge_id, [{"job_id": job["job_id"], "state": "running"}])
        self.gateway.reconcile_bridge_jobs(self.bridge_id, [])
        self.assertEqual(self.gateway._read_registry()["jobs"][job["job_id"]]["state"], "bridge_offline")
        with self.assertRaises(PlatformError) as raised:
            self.gateway.dispatch_bridge_job(self.job(key="other"), wait_seconds=1)
        self.assertEqual(raised.exception.code, "DEVICE_LEASE_BUSY")

    def test_legacy_offline_with_progress_audit_is_not_recovered(self):
        job = self.legacy_offline_job()
        self.gateway._append_bridge_event("device_job_progress", self.bridge_id,
            {"job_id": job["job_id"], "progress": {"stage": "hdc_started"}})
        self.gateway.reconcile_bridge_jobs(self.bridge_id, [])
        self.assertEqual(self.gateway._read_registry()["jobs"][job["job_id"]]["state"], "bridge_offline")

    def test_legacy_offline_with_a_reoffer_race_is_not_recovered(self):
        job = self.legacy_offline_job()
        self.gateway._append_bridge_event("bridge_reconciled", self.bridge_id,
            {"known_job_count": 0, "reoffered_job_ids": [job["job_id"]]})
        self.gateway.reconcile_bridge_jobs(self.bridge_id, [])
        self.assertEqual(self.gateway._read_registry()["jobs"][job["job_id"]]["state"], "bridge_offline")

    def test_legacy_offline_without_audit_is_not_recovered(self):
        job = self.legacy_offline_job()
        self.gateway.event_log.unlink()
        self.gateway.reconcile_bridge_jobs(self.bridge_id, [])
        self.assertEqual(self.gateway._read_registry()["jobs"][job["job_id"]]["state"], "bridge_offline")

    def test_repeated_expired_lease_resolution_converges_after_restart(self):
        self.expire(self.lease["lease_id"])
        results = []
        def resolve():
            results.append(self.gateway.ensure_active_lease(self.lease["lease_id"], project_id="test",
                bridge_id=self.bridge_id, logical_device_id=self.target))
        workers = [threading.Thread(target=resolve) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        self.assertEqual(len({item["effective_lease_id"] for item in results}), 1)
        restarted = DeviceGateway(workspace_root=self.gateway.workspace_root, state_root=self.gateway.state_root)
        reused = restarted.create_lease(project_id="test", bridge_id=self.bridge_id, logical_device_id=self.target)
        self.assertEqual(reused["lease_id"], results[0]["effective_lease_id"])
        self.assertEqual(len(restarted._read_registry()["leases"]), 2)

    def test_foreign_project_and_target_cannot_reuse_lease(self):
        with self.assertRaises(PlatformError) as raised:
            self.create_lease(project_id="another")
        self.assertEqual(raised.exception.code, "DEVICE_LEASE_UNAUTHORIZED")
        with self.assertRaises(PlatformError) as raised:
            self.gateway.ensure_active_lease(self.lease["lease_id"], project_id="test",
                bridge_id=self.bridge_id, logical_device_id="device_another")
        self.assertEqual(raised.exception.code, "DEVICE_LEASE_UNAUTHORIZED")
