"""Cancellation must identify the historical job's phone, not just its Bridge."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.core import PlatformError
from backend.device_gateway import DeviceGateway
from backend.runtime_action_router import PlatformRuntimeActionRouter


JOB_ID = "devjob_" + "1" * 24
BRIDGE = {"connection_mode": "bridge", "bridge_id": "bridge_fixture", "device_serial": "phone_B"}
LOCAL = {"connection_mode": "local", "bridge_id": "", "device_serial": "phone_B"}


class CaptureCancelTargetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.gateway = DeviceGateway(workspace_root=root, state_root=root / "state")
        self.gateway.cancel_bridge_job = Mock(return_value={"status": "cancel_requested", "job_id": JOB_ID})
        self.gateway.local_executor.cancel = Mock(return_value={"status": "acknowledged", "job_id": JOB_ID})
        self.router = PlatformRuntimeActionRouter(
            artifact_store=Mock(), build_worker=Mock(), device_gateway=self.gateway,
            default_source_connector=lambda: {}, bridge_artifact_url=lambda *_: "unused",
        )

    @staticmethod
    def run(target):
        return {"project_id": "project_fixture", "run_id": "run_current", "context": {
            "page_context": {"active_view": "scenario_capture", "scenario_capture": {"selected_device": target}},
        }}

    def record(self, target=BRIDGE, **overrides):
        job = {
            "job_id": JOB_ID, "project_id": "project_fixture", "agent_run_id": "run_historical",
            "bridge_id": target["bridge_id"],
            "logical_device_id": DeviceGateway.logical_device_id(target["device_serial"]),
            "payload": {"device_serial": target["device_serial"]},
            **overrides,
        }
        registry = self.gateway._read_registry()
        registry["jobs"][JOB_ID] = {"job": job, "state": "accepted_waiting_result"}
        self.gateway._write_registry(registry)

    def cancel(self, target=BRIDGE):
        return self.router.execute("device.cancel_job", {"job_id": JOB_ID}, self.run(target))

    def assert_blocked(self, code, target=BRIDGE):
        with self.assertRaises(PlatformError) as caught:
            self.cancel(target)
        self.assertEqual(caught.exception.code, code)
        self.gateway.cancel_bridge_job.assert_not_called()
        self.gateway.local_executor.cancel.assert_not_called()

    def test_matching_historical_job_on_selected_phone_can_be_cancelled(self):
        self.record()
        self.assertEqual(self.cancel()["status"], "cancel_requested")
        self.gateway.cancel_bridge_job.assert_called_once_with(bridge_id="bridge_fixture", job_id=JOB_ID)

    def test_another_phone_on_same_bridge_is_rejected_even_without_payload_serial(self):
        self.record(logical_device_id=DeviceGateway.logical_device_id("phone_A"), payload={})
        self.assert_blocked("SCENARIO_DEVICE_TARGET_MISMATCH")

    def test_matching_logical_identity_with_conflicting_serial_is_rejected(self):
        self.record(payload={"device_serial": "phone_A"})
        self.assert_blocked("SCENARIO_DEVICE_TARGET_MISMATCH")

    def test_another_project_is_rejected(self):
        self.record(project_id="another_project")
        self.assert_blocked("SCENARIO_DEVICE_TARGET_MISMATCH")

    def test_wrong_bridge_and_absent_records_do_not_send_cancellation(self):
        self.assert_blocked("SCENARIO_DEVICE_JOB_OWNER_UNVERIFIED")
        self.record(bridge_id="bridge_other")
        self.assert_blocked("SCENARIO_DEVICE_JOB_OWNER_UNVERIFIED")

    def test_incomplete_identity_is_not_treated_as_permission_to_cancel(self):
        for missing in ({"project_id": ""}, {"logical_device_id": ""}):
            with self.subTest(missing=missing):
                self.record(**missing)
                self.assert_blocked("SCENARIO_DEVICE_JOB_OWNER_UNVERIFIED")

    def test_local_without_durable_job_identity_is_not_acknowledged(self):
        self.assert_blocked("SCENARIO_DEVICE_JOB_OWNER_UNVERIFIED", LOCAL)
        self.record(LOCAL, payload={})
        self.assert_blocked("SCENARIO_DEVICE_JOB_OWNER_UNVERIFIED", LOCAL)

    def test_local_record_requires_matching_project_and_serial(self):
        for overrides in ({"payload": {"device_serial": "phone_A"}}, {"device_serial": "phone_A"}, {"project_id": "another_project"}):
            with self.subTest(overrides=overrides):
                self.record(LOCAL, **overrides)
                self.assert_blocked("SCENARIO_DEVICE_TARGET_MISMATCH", LOCAL)
        self.record(LOCAL, payload={}, device_serial="phone_B")
        self.assertEqual(self.cancel(LOCAL)["status"], "acknowledged")
        self.gateway.local_executor.cancel.assert_called_once_with(JOB_ID)

    def test_non_capture_context_preserves_existing_remote_and_local_behavior(self):
        run = {"project_id": "project_fixture", "run_id": "run_general", "context": {}}
        with patch.object(self.gateway, "verify_job_owner", side_effect=AssertionError("capture guard should not run")):
            self.router.execute("device.cancel_job", {"job_id": JOB_ID}, run)
            self.router.execute("device.cancel_job", {"job_id": JOB_ID, "bridge_id": "bridge_fixture",
                "logical_device_id": DeviceGateway.logical_device_id("phone_A")}, run)
        self.gateway.local_executor.cancel.assert_called_once_with(JOB_ID)
        self.gateway.cancel_bridge_job.assert_called_once_with(bridge_id="bridge_fixture", job_id=JOB_ID)


if __name__ == "__main__":
    unittest.main()
