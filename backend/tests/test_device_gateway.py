from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.core import PlatformError
from backend.device_gateway import HTTP_POLL_PROTOCOL_VERSION, DeviceGateway, LocalHdcDeviceExecutor


class DeviceGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.gateway = DeviceGateway(workspace_root=root, state_root=root / "state")
        pairing = self.gateway.create_pairing_code(project_id="local-default")
        registered = self.gateway.register_bridge(
            pairing_code=str(pairing["pairing_code"]),
            display_name="test bridge",
            capabilities={"protocol_version": "device-bridge-ws/v1", "actions": []},
        )
        self.bridge_id = str(registered["bridge_id"])
        self.bridge_secret = str(registered["bridge_secret"])
        self.sent: list[dict[str, object]] = []
        self.gateway.connect_bridge(
            self.bridge_id,
            self.bridge_secret,
            capabilities={
                "protocol_version": "device-bridge-ws/v1",
                "actions": ["device.discover", "device.probe", "device.deploy_artifact", "device.execute_shell", "device.collect_artifact", "device.cancel_job"],
            },
            send=self.sent.append,
        )
        self.logical_device_id = "device_test_phone"
        self.lease = self.gateway.create_lease(
            project_id="local-default",
            bridge_id=self.bridge_id,
            logical_device_id=self.logical_device_id,
        )

    def _job(self, *, action: str = "device.execute_shell", key: str = "same-command") -> dict[str, object]:
        return self.gateway._bridge_job(
            project_id="local-default",
            agent_run_id="run_aaaaaaaaaaaaaaaaaaaaaaaa",
            bridge_id=self.bridge_id,
            lease_id=str(self.lease["lease_id"]),
            logical_device_id=self.logical_device_id,
            action=action,
            payload={"device_serial": "test-device", "command": "echo verified"},
            idempotency_key=key,
            timeout_seconds=30,
        )

    def test_signed_job_reports_result_and_audits_without_secret(self) -> None:
        job = self._job()
        response: dict[str, object] = {}

        def dispatch() -> None:
            response.update(self.gateway.dispatch_bridge_job(job, wait_seconds=2))

        worker = threading.Thread(target=dispatch)
        worker.start()
        deadline = time.monotonic() + 1
        while not self.sent and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.sent)
        offered = self.sent[-1]
        self.assertEqual(offered["type"], "job.offer")
        self.gateway.mark_bridge_job_accepted(self.bridge_id, str(job["job_id"]))
        self.gateway.report_bridge_progress(self.bridge_id, {"job_id": job["job_id"], "request_id": job["request_id"], "progress": {"stage": "hdc_started"}})
        self.gateway.report_bridge_result(
            self.bridge_id,
            {
                "job_id": job["job_id"],
                "request_id": job["request_id"],
                "result": {"status": "succeeded", "device_serial": "test-device", "execution": {"return_code": 0}},
            },
        )
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(response["result"]["status"], "succeeded")
        events = self.gateway.event_log.read_text(encoding="utf-8")
        self.assertIn("device_job_issued", events)
        self.assertIn("device_job_progress", events)
        self.assertNotIn(self.bridge_secret, events)

    def test_http_poll_transport_delivers_and_completes_a_signed_job(self) -> None:
        connection_id = "connection_" + "b" * 32
        capabilities = {
            "protocol_version": HTTP_POLL_PROTOCOL_VERSION,
            "transport": "http_poll",
            "actions": [
                "device.discover",
                "device.probe",
                "device.deploy_artifact",
                "device.execute_shell",
                "device.collect_artifact",
                "device.cancel_job",
            ],
        }
        connected = self.gateway.poll_bridge_messages(
            self.bridge_id,
            self.bridge_secret,
            connection_id=connection_id,
            capabilities=capabilities,
            reconcile_jobs=[],
            max_messages=32,
        )
        self.assertTrue(connected["new_connection"])
        self.assertEqual(connected["protocol_version"], HTTP_POLL_PROTOCOL_VERSION)

        job = self._job(key="http-poll-roundtrip")
        response: dict[str, object] = {}

        worker = threading.Thread(
            target=lambda: response.update(self.gateway.dispatch_bridge_job(job, wait_seconds=3)),
        )
        worker.start()
        messages: list[dict[str, object]] = []
        deadline = time.monotonic() + 2
        while not messages and time.monotonic() < deadline:
            polled = self.gateway.poll_bridge_messages(
                self.bridge_id,
                self.bridge_secret,
                connection_id=connection_id,
                capabilities=capabilities,
                reconcile_jobs=None,
                max_messages=32,
            )
            messages = polled["messages"]
            if not messages:
                time.sleep(0.01)

        self.assertEqual(messages[0]["type"], "job.offer")
        self.assertEqual(messages[0]["job"]["job_id"], job["job_id"])
        self.gateway.mark_bridge_job_accepted(self.bridge_id, str(job["job_id"]))
        self.gateway.report_bridge_result(
            self.bridge_id,
            {
                "job_id": job["job_id"],
                "request_id": job["request_id"],
                "result": {"status": "succeeded", "execution": {"return_code": 0}},
            },
        )
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(response["result"]["status"], "succeeded")
        listed = next(item for item in self.gateway.list_bridges(project_id="local-default") if item["bridge_id"] == self.bridge_id)
        self.assertEqual(listed["transport"], "http_poll")

    def test_stale_websocket_disconnect_cannot_remove_http_poll_session(self) -> None:
        self.gateway.poll_bridge_messages(
            self.bridge_id,
            self.bridge_secret,
            connection_id="connection_" + "c" * 32,
            capabilities={"protocol_version": HTTP_POLL_PROTOCOL_VERSION, "actions": ["device.discover"]},
            reconcile_jobs=[],
            max_messages=1,
        )

        self.gateway.disconnect_bridge(self.bridge_id, transport="websocket")

        listed = next(item for item in self.gateway.list_bridges(project_id="local-default") if item["bridge_id"] == self.bridge_id)
        self.assertEqual(listed["connection_state"], "connected")
        self.assertEqual(listed["transport"], "http_poll")

    def test_http_poll_authenticates_once_and_rejects_bad_secret_without_replacing_session(self) -> None:
        payload = {
            "connection_id": "connection_" + "d" * 32,
            "capabilities": {"protocol_version": HTTP_POLL_PROTOCOL_VERSION, "actions": ["device.discover"]},
            "reconcile_jobs": [],
            "max_messages": 1,
        }
        with patch.object(self.gateway, "authenticate_bridge", wraps=self.gateway.authenticate_bridge) as authenticate:
            response = self.gateway.poll_bridge_messages(self.bridge_id, self.bridge_secret, **payload)
        self.assertTrue(response["new_connection"])
        self.assertEqual(authenticate.call_count, 1)
        existing_session = self.gateway._sessions[self.bridge_id]
        with self.assertRaises(PlatformError) as raised:
            self.gateway.poll_bridge_messages(self.bridge_id, "wrong-secret", **{
                **payload, "connection_id": "connection_" + "e" * 32,
            })
        self.assertEqual(raised.exception.code, "BRIDGE_AUTH_INVALID")
        self.assertIs(self.gateway._sessions[self.bridge_id], existing_session)

    def test_reconnect_batches_256_receipts_and_preserves_other_bridge_and_terminal_states(self) -> None:
        other = self.gateway.register_trusted_bridge(project_id="local-default", display_name="other", capabilities={})
        registry = self.gateway._read_registry()
        known = []
        for index in range(256):
            job_id = f"devjob_{index:024x}"
            registry["jobs"][job_id] = {
                "job": {"job_id": job_id, "bridge_id": self.bridge_id},
                "state": "succeeded" if index % 2 else "state_unknown",
                "result": {"stdout": "historical result " * 64},
            }
            known.append({"job_id": job_id, "state": "succeeded"})
        foreign_id = "devjob_" + "f" * 24
        registry["jobs"][foreign_id] = {
            "job": {"job_id": foreign_id, "bridge_id": other["bridge_id"]}, "state": "state_unknown",
        }
        known.extend([{"job_id": foreign_id}, {"job_id": "devjob_" + "a" * 24}])
        self.gateway._write_registry(registry)
        with patch.object(self.gateway, "_read_registry", wraps=self.gateway._read_registry) as read, \
             patch.object(self.gateway, "_write_registry", wraps=self.gateway._write_registry) as write:
            offers = self.gateway.reconcile_bridge_jobs(self.bridge_id, known)
        self.assertEqual(offers, [])
        self.assertEqual(read.call_count, 1)
        self.assertEqual(write.call_count, 1)
        after = self.gateway._read_registry()
        for item in known[:256]:
            job_id = item["job_id"]
            self.assertEqual(after["jobs"][job_id]["delivery_state"], "received")
            self.assertTrue(after["jobs"][job_id]["bridge_received_at"])
            self.assertEqual(after["jobs"][job_id]["state"], registry["jobs"][job_id]["state"])
            self.assertEqual(after["jobs"][job_id]["result"], registry["jobs"][job_id]["result"])
        self.assertNotIn("bridge_received_at", after["jobs"][foreign_id])
        self.assertIn("bridge_reconciled", self.gateway.event_log.read_text(encoding="utf-8"))

        with patch.object(self.gateway, "_read_registry", wraps=self.gateway._read_registry) as read, \
             patch.object(self.gateway, "_write_registry", wraps=self.gateway._write_registry) as write:
            self.assertEqual(self.gateway.reconcile_bridge_jobs(self.bridge_id, known), [])
        self.assertEqual(read.call_count, 1)
        self.assertEqual(write.call_count, 0)
        self.assertEqual(self.gateway._read_registry(), after)

    def test_initial_http_poll_has_bounded_registry_io_with_256_historical_jobs(self) -> None:
        registry = self.gateway._read_registry()
        known = []
        for index in range(256):
            job_id = f"devjob_{index:024x}"
            registry["jobs"][job_id] = {
                "job": {"job_id": job_id, "bridge_id": self.bridge_id}, "state": "succeeded",
            }
            known.append({"job_id": job_id, "state": "succeeded"})
        self.gateway._write_registry(registry)
        with patch.object(self.gateway, "_read_registry", wraps=self.gateway._read_registry) as read, \
             patch.object(self.gateway, "_write_registry", wraps=self.gateway._write_registry) as write:
            response = self.gateway.poll_bridge_messages(
                self.bridge_id,
                self.bridge_secret,
                connection_id="connection_" + "f" * 32,
                capabilities={"protocol_version": HTTP_POLL_PROTOCOL_VERSION, "actions": ["device.discover"]},
                reconcile_jobs=known,
                max_messages=32,
            )
        self.assertTrue(response["new_connection"])
        self.assertEqual(response["messages"], [])
        self.assertEqual(read.call_count, 4)
        self.assertEqual(write.call_count, 2)

    def test_pairing_code_is_hashed_in_private_registry_and_redacted_from_events(self) -> None:
        pairing = self.gateway.create_pairing_code(project_id="local-default")
        raw_code = str(pairing["pairing_code"])
        registry_text = (Path(self.tempdir.name) / "state" / "device-gateway" / "registry.json").read_text(encoding="utf-8")

        self.assertNotIn(raw_code, registry_text)
        self.gateway._append_bridge_event(
            "test_pairing_redaction",
            self.bridge_id,
            {"pairing_code": raw_code, "bridge_secret": self.bridge_secret},
        )
        event_text = self.gateway.event_log.read_text(encoding="utf-8")
        self.assertNotIn(raw_code, event_text)
        self.assertNotIn(self.bridge_secret, event_text)

    def test_trusted_single_user_registration_needs_no_pairing_code(self) -> None:
        registered = self.gateway.register_trusted_bridge(
            project_id="local-default",
            display_name="zero-touch bridge",
            capabilities={"protocol_version": "device-bridge-ws/v1", "actions": ["device.discover"]},
        )

        self.assertEqual(registered["registration_mode"], "trusted_single_user")
        self.assertTrue(registered["bridge_id"])
        self.assertTrue(registered["bridge_secret"])
        listed = next(
            item
            for item in self.gateway.list_bridges(project_id="local-default")
            if item["bridge_id"] == registered["bridge_id"]
        )
        self.assertEqual(listed["registration_mode"], "trusted_single_user")
        self.assertNotIn("bridge_secret", listed)

    def test_bridge_list_exposes_non_secret_runtime_diagnostics(self) -> None:
        diagnostics = {
            "schema_version": "device_bridge_diagnostics/v1",
            "status": "ready",
            "hdc": {"target_count": 1, "configured_path_exists": True},
            "spool": {"writable": True},
            "issues": [],
        }
        self.gateway.connect_bridge(
            self.bridge_id,
            self.bridge_secret,
            capabilities={
                "protocol_version": "device-bridge-ws/v1",
                "implementation_version": "1.2.0",
                "actions": ["device.discover"],
                "runtime_diagnostics": diagnostics,
            },
            send=self.sent.append,
        )

        listed = next(item for item in self.gateway.list_bridges(project_id="local-default") if item["bridge_id"] == self.bridge_id)
        self.assertEqual(listed["capabilities"]["runtime_diagnostics"], diagnostics)
        self.assertNotIn("bridge_secret", listed)

    def test_unknown_prior_job_is_not_reoffered(self) -> None:
        first = self._job(action="device.probe", key="state-unknown")
        with self.assertRaisesRegex(PlatformError, "状态未知"):
            self.gateway.dispatch_bridge_job(first, wait_seconds=1)

        repeated = self._job(action="device.probe", key="state-unknown")
        with self.assertRaisesRegex(PlatformError, "前一次执行尚未形成终态"):
            self.gateway.dispatch_bridge_job(repeated, wait_seconds=1)

        offers = self.gateway.reconcile_bridge_jobs(self.bridge_id, [])
        self.assertFalse(any(item["idempotency_key"] == "state-unknown" for item in offers))

    def test_capability_rejection_is_terminal_not_bridge_offline(self) -> None:
        self.gateway.connect_bridge(
            self.bridge_id,
            self.bridge_secret,
            capabilities={"protocol_version": "device-bridge-ws/v1", "actions": ["device.discover"]},
            send=self.sent.append,
        )
        job = self._job(key="unsupported-action")

        with self.assertRaisesRegex(PlatformError, "未声明支持"):
            self.gateway.dispatch_bridge_job(job, wait_seconds=1)

        registry = self.gateway._read_registry()
        entry = registry["jobs"][job["job_id"]]
        self.assertEqual(entry["state"], "failed")

    def test_remote_discovery_bootstraps_before_a_device_lease_exists(self) -> None:
        job = self.gateway._bridge_job(
            project_id="local-default",
            agent_run_id="run_aaaaaaaaaaaaaaaaaaaaaaaa",
            bridge_id=self.bridge_id,
            lease_id="",
            logical_device_id="",
            action="device.discover",
            payload={},
            idempotency_key="remote-discovery",
            timeout_seconds=30,
        )

        self.assertIsNone(job["device_lease_id"])
        self.assertEqual(job["logical_device_id"], "device_discovery")
        self.assertTrue(self.gateway.verify_job_signature(self.bridge_id, job))

    def test_expired_lease_is_replaced_once_and_old_id_resolves_to_replacement(self) -> None:
        original_id = str(self.lease["lease_id"])
        registry = self.gateway._read_registry()
        registry["leases"][original_id]["expires_at_epoch"] = time.time() - 1
        self.gateway._write_registry(registry)

        first = self.gateway.ensure_active_lease(
            original_id,
            project_id="local-default",
            bridge_id=self.bridge_id,
            logical_device_id=self.logical_device_id,
        )
        second = self.gateway.ensure_active_lease(
            original_id,
            project_id="local-default",
            bridge_id=self.bridge_id,
            logical_device_id=self.logical_device_id,
        )

        self.assertTrue(first["replaced"])
        self.assertEqual(first["replacement_reason"], "expired")
        self.assertNotEqual(first["effective_lease_id"], original_id)
        self.assertEqual(second["effective_lease_id"], first["effective_lease_id"])
        self.assertEqual(second["replacement_reason"], "previously_replaced")
        self.assertEqual(len(self.gateway._read_registry()["leases"]), 2)
        self.assertIn("device_lease_replaced", self.gateway.event_log.read_text(encoding="utf-8"))

    def test_fresh_lease_is_reusable_but_near_expiry_lease_is_not(self) -> None:
        lease_id = str(self.lease["lease_id"])
        self.assertTrue(
            self.gateway.lease_is_reusable(
                lease_id,
                project_id="local-default",
                bridge_id=self.bridge_id,
                logical_device_id=self.logical_device_id,
                minimum_remaining_seconds=60,
            )
        )

        registry = self.gateway._read_registry()
        registry["leases"][lease_id]["expires_at_epoch"] = time.time() + 30
        self.gateway._write_registry(registry)

        self.assertFalse(
            self.gateway.lease_is_reusable(
                lease_id,
                project_id="local-default",
                bridge_id=self.bridge_id,
                logical_device_id=self.logical_device_id,
                minimum_remaining_seconds=60,
            )
        )

    def test_late_success_after_cancel_does_not_resurrect_job(self) -> None:
        job = self._job(key="late-after-cancel")
        self.gateway._record_issued_job(job)
        self.gateway._record_job_state(str(job["job_id"]), "cancelled")

        self.gateway.report_bridge_result(
            self.bridge_id,
            {
                "job_id": job["job_id"],
                "request_id": job["request_id"],
                "result": {"status": "succeeded", "execution": {"return_code": 0}},
            },
        )

        registry = self.gateway._read_registry()
        self.assertEqual(registry["jobs"][job["job_id"]]["state"], "cancelled")
        self.assertIn("device_job_late_result_after_cancel", self.gateway.event_log.read_text(encoding="utf-8"))

    def test_unknown_spooled_result_is_explicitly_stale_not_unauthorized(self) -> None:
        with self.assertRaises(PlatformError) as raised:
            self.gateway.report_bridge_result(
                self.bridge_id,
                {
                    "job_id": "devjob_" + "f" * 24,
                    "request_id": "req_old",
                    "result": {"status": "succeeded"},
                },
            )

        self.assertEqual(raised.exception.code, "BRIDGE_JOB_RESULT_STALE")
        self.assertEqual(raised.exception.status_code, 410)

    def test_remote_deploy_success_without_device_hash_is_downgraded(self) -> None:
        remote_path = "/data/local/tmp/hetero_runs/contract/experiment.bin"
        artifact_sha = "a" * 64
        job = self.gateway._bridge_job(
            project_id="local-default",
            agent_run_id="run_aaaaaaaaaaaaaaaaaaaaaaaa",
            bridge_id=self.bridge_id,
            lease_id=str(self.lease["lease_id"]),
            logical_device_id=self.logical_device_id,
            action="device.deploy_artifact",
            payload={
                "device_serial": "test-device",
                "remote_path": remote_path,
                "artifact_sha256": artifact_sha,
            },
            idempotency_key="deploy-without-proof",
            timeout_seconds=30,
        )
        self.gateway._record_issued_job(job)

        self.gateway.report_bridge_result(
            self.bridge_id,
            {
                "job_id": job["job_id"],
                "request_id": job["request_id"],
                "result": {
                    "status": "succeeded",
                    "device_serial": "test-device",
                    "remote_path": remote_path,
                    "artifact_sha256": artifact_sha,
                },
            },
        )

        entry = self.gateway._read_registry()["jobs"][job["job_id"]]
        output = entry["result"]["result"]
        self.assertEqual(entry["state"], "failed")
        self.assertFalse(output["deployment_verified"])
        self.assertEqual(output["stage"], "deployment_contract_verification")
        self.assertEqual(output["error"]["code"], "DEVICE_DEPLOYMENT_NOT_VERIFIED")

    def test_remote_deploy_success_with_matching_device_hash_is_accepted(self) -> None:
        remote_path = "/data/local/tmp/hetero_runs/contract/experiment.bin"
        artifact_sha = "b" * 64
        job = self.gateway._bridge_job(
            project_id="local-default",
            agent_run_id="run_aaaaaaaaaaaaaaaaaaaaaaaa",
            bridge_id=self.bridge_id,
            lease_id=str(self.lease["lease_id"]),
            logical_device_id=self.logical_device_id,
            action="device.deploy_artifact",
            payload={
                "device_serial": "test-device",
                "remote_path": remote_path,
                "artifact_sha256": artifact_sha,
            },
            idempotency_key="deploy-with-proof",
            timeout_seconds=30,
        )
        self.gateway._record_issued_job(job)

        self.gateway.report_bridge_result(
            self.bridge_id,
            {
                "job_id": job["job_id"],
                "request_id": job["request_id"],
                "result": {
                    "status": "succeeded",
                    "device_serial": "test-device",
                    "remote_path": remote_path,
                    "artifact_sha256": artifact_sha,
                    "remote_sha256": artifact_sha,
                    "deployment_verified": True,
                    "executable": True,
                },
            },
        )

        entry = self.gateway._read_registry()["jobs"][job["job_id"]]
        self.assertEqual(entry["state"], "succeeded")
        self.assertTrue(entry["result"]["result"]["deployment_verified"])

    def test_progress_is_mirrored_to_owning_run_without_bridge_secret(self) -> None:
        observed: list[dict[str, object]] = []
        self.gateway.set_run_event_sink(observed.append)
        job = self._job(action="device.probe", key="progress-mirror")
        self.gateway._record_issued_job(job)

        self.gateway.report_bridge_progress(
            self.bridge_id,
            {
                "job_id": job["job_id"],
                "request_id": job["request_id"],
                "progress": {"stage": "hdc_started", "detail_zh": "Bridge 已开始探测设备。"},
            },
        )

        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["agent_run_id"], "run_aaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(observed[0]["action_id"], "device.probe")
        self.assertNotIn("bridge_secret", observed[0])

    def test_cancel_run_requests_bridge_stop_and_prevents_reoffer(self) -> None:
        job = self._job(key="cancel-run")
        self.gateway._record_issued_job(job)

        result = self.gateway.cancel_agent_run("run_aaaaaaaaaaaaaaaaaaaaaaaa")

        self.assertEqual(result["cancelled_job_ids"], [job["job_id"]])
        self.assertEqual(self.sent[-1], {"type": "job.cancel", "job_id": job["job_id"]})
        registry = self.gateway._read_registry()
        self.assertEqual(registry["jobs"][job["job_id"]]["state"], "cancel_requested")
        self.assertFalse(any(item["job_id"] == job["job_id"] for item in self.gateway.reconcile_bridge_jobs(self.bridge_id, [])))

    @unittest.skipUnless(os.name == "posix" and Path("/bin/sh").is_file(), "requires a POSIX shell for tracked-process contract coverage")
    def test_local_executor_cancellation_terminates_active_process_group(self) -> None:
        """The same Run cancellation must stop local HDC compatibility work.

        The test exercises the tracked argv runner with ``/bin/sh`` only as a
        harmless stand-in for the administrator-configured HDC executable. It
        never uses a host shell in product code; the runner still launches the
        configured executable through an argv vector and a new session.
        """

        executor = LocalHdcDeviceExecutor(Path(self.tempdir.name))
        observed: dict[str, object] = {}
        run_id = "run_aaaaaaaaaaaaaaaaaaaaaaaa"

        def run_command() -> None:
            observed.update(
                executor._run_hdc_tracked(
                    Path("/bin/sh"),
                    ("-c", "sleep 20"),
                    timeout_seconds=30,
                    agent_run_id=run_id,
                )
            )

        worker = threading.Thread(target=run_command)
        worker.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with executor._lock:
                if executor._active_processes.get(run_id):
                    break
            time.sleep(0.01)
        cancellation = executor.cancel_run(run_id)
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(cancellation["status"], "cancel_requested")
        self.assertGreaterEqual(int(cancellation["active_process_count"]), 1)
        self.assertTrue(observed["cancelled"])


if __name__ == "__main__":
    unittest.main()
