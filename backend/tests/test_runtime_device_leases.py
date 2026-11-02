from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.core import ArtifactStore, PlatformError
from backend.device_gateway import DeviceGateway
from backend.runtime_action_router import PlatformRuntimeActionRouter


class RuntimeDeviceLeaseTests(unittest.TestCase):
    """Exercise router identity and acquisition boundaries without a device."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.gateway = DeviceGateway(workspace_root=self.root, state_root=self.root / "gateway")
        self.store = ArtifactStore(self.root / "artifacts")
        self.serial = "test-phone-001"
        self.logical_id = self.gateway.logical_device_id(self.serial)
        self.bridge_id = self._bridge("local-default")
        self.run = {"run_id": "run_" + "a" * 24, "project_id": "local-default", "context": {}}
        binary = self.root / "experiment.bin"
        binary.write_bytes(b"fixture")
        self.worker = Mock()
        self.worker.binary_path.return_value = binary
        self.router = PlatformRuntimeActionRouter(
            artifact_store=self.store,
            build_worker=self.worker,
            device_gateway=self.gateway,
            default_source_connector=lambda: {"connector_id": "test", "snapshot": {}},
            bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
        )
        self.probe = self._artifact("device_probe", {"device": {"serial": self.serial}})
        self.build = self._artifact("build_result", {
            "build_identity": "b" * 64,
            "manifest": {"binary": {"sha256": "c" * 64}},
        })
        dispatch = patch.object(self.gateway, "dispatch_bridge_job", side_effect=self._respond)
        self.dispatch = dispatch.start()
        self.addCleanup(dispatch.stop)
        grant = patch.object(self.gateway, "issue_download_grant", return_value="fixture-token")
        grant.start()
        self.addCleanup(grant.stop)

    def _bridge(self, project_id: str) -> str:
        pairing = self.gateway.create_pairing_code(project_id=project_id)
        result = self.gateway.register_bridge(
            pairing_code=pairing["pairing_code"], display_name="isolated-test", capabilities={},
        )
        return result["bridge_id"]

    def _artifact(self, kind: str, payload: dict) -> dict:
        return self.store.write(kind, payload, source_connector_id="test", source_binding={},
                                producer={"name": "test", "version": "1"})

    def _input(self, action: str) -> dict:
        value = {"bridge_id": self.bridge_id, "logical_device_id": self.logical_id}
        if action == "device.probe":
            value["device_serial"] = self.serial
        else:
            value["device_probe_artifact_id"] = self.probe["artifact_id"]
        if action == "device.deploy_artifact":
            value["build_result_artifact_id"] = self.build["artifact_id"]
        if action == "device.execute_shell":
            value["command"] = "echo fixture"
        if action == "device.collect_artifact":
            value["remote_path"] = "/data/local/tmp/fixture.txt"
        return value

    def _respond(self, job: dict, **_: object) -> dict:
        payload = job["payload"]
        result = {"status": "succeeded"}
        if job["action"] == "device.discover":
            result["devices"] = [{"serial": self.serial, "logical_device_id": self.logical_id}]
        if job["action"] == "device.probe":
            result["device"] = {"serial": self.serial}
        if job["action"] == "device.deploy_artifact":
            result.update(self._deployment(payload))
        return {"job_id": job["job_id"], "result": result}

    @staticmethod
    def _deployment(payload: dict) -> dict:
        return {"status": "succeeded", "deployment_verified": True, "executable": True,
                "device_serial": payload["device_serial"], "remote_path": payload["remote_path"],
                "artifact_sha256": payload["artifact_sha256"], "remote_sha256": payload["artifact_sha256"]}

    def test_all_remote_actions_acquire_once_and_report_resolution_without_agent_lease(self) -> None:
        actions = ("device.probe", "device.execute_shell", "device.deploy_artifact", "device.collect_artifact")
        for action in actions:
            with self.subTest(action=action), patch.object(
                self.router, "_resolve_remote_lease", wraps=self.router._resolve_remote_lease,
            ) as resolve, patch.object(self.gateway, "create_lease", wraps=self.gateway.create_lease) as create:
                result = self.router.execute(action, self._input(action), self.run)
                self.assertEqual(result["status"], "succeeded")
                resolution = result["lease_resolution"]
                self.assertTrue(resolution["automatic"])
                self.assertEqual(resolution["requested_lease_id"], "")
                job = self.dispatch.call_args.args[0]
                self.assertEqual(job["device_lease_id"], resolution["effective_lease_id"])
                self.assertEqual(job["logical_device_id"], self.logical_id)
                self.assertEqual(resolve.call_count, 1)
                self.assertEqual(create.call_count, 1)

    def test_selector_read_is_pure_and_requires_explicit_target(self) -> None:
        value = self._input("device.probe")
        self.assertEqual(self.router._remote_selector(value, self.run), (self.bridge_id, "", self.logical_id))
        self.router._remote_selector(value, self.run)
        self.assertEqual(self.gateway._read_registry()["leases"], {})
        with self.assertRaises(PlatformError) as raised:
            self.router._remote_selector({"bridge_id": self.bridge_id}, self.run)
        self.assertEqual(raised.exception.code, "DEVICE_BRIDGE_SELECTOR_INVALID")

    def test_explicit_foreign_project_lease_is_rejected_without_replacement(self) -> None:
        other_bridge = self._bridge("other-project")
        lease = self.gateway.create_lease(project_id="other-project", bridge_id=other_bridge,
                                          logical_device_id=self.logical_id)
        value = {**self._input("device.probe"), "device_lease_id": lease["lease_id"]}
        with patch.object(self.gateway, "create_lease", wraps=self.gateway.create_lease) as create:
            with self.assertRaises(PlatformError) as raised:
                self.router.execute("device.probe", value, self.run)
        self.assertEqual(raised.exception.code, "DEVICE_LEASE_UNAUTHORIZED")
        create.assert_not_called()
        self.dispatch.assert_not_called()

    def test_unknown_explicit_lease_is_not_silently_replaced(self) -> None:
        value = {**self._input("device.probe"), "device_lease_id": "lease_" + "f" * 24}
        with self.assertRaises(PlatformError) as raised:
            self.router.execute("device.probe", value, self.run)
        self.assertEqual(raised.exception.code, "DEVICE_LEASE_UNAUTHORIZED")
        self.assertEqual(self.gateway._read_registry()["leases"], {})

    def test_explicit_lease_for_another_device_is_rejected(self) -> None:
        lease = self.gateway.create_lease(
            project_id="local-default", bridge_id=self.bridge_id,
            logical_device_id=self.gateway.logical_device_id("another-phone"),
        )
        value = {**self._input("device.probe"), "device_lease_id": lease["lease_id"]}
        with patch.object(self.gateway, "create_lease", wraps=self.gateway.create_lease) as create:
            with self.assertRaises(PlatformError) as raised:
                self.router.execute("device.probe", value, self.run)
        self.assertEqual(raised.exception.code, "DEVICE_LEASE_UNAUTHORIZED")
        create.assert_not_called()
        self.dispatch.assert_not_called()

    def test_explicit_expired_matching_lease_still_renews(self) -> None:
        lease = self.gateway.create_lease(project_id="local-default", bridge_id=self.bridge_id,
                                          logical_device_id=self.logical_id)
        registry = self.gateway._read_registry()
        registry["leases"][lease["lease_id"]]["expires_at_epoch"] = 0
        self.gateway._write_registry(registry)
        value = {**self._input("device.probe"), "device_lease_id": lease["lease_id"]}
        result = self.router.execute("device.probe", value, self.run)
        resolution = result["lease_resolution"]
        self.assertFalse(resolution["automatic"])
        self.assertTrue(resolution["replaced"])
        self.assertEqual(resolution["requested_lease_id"], lease["lease_id"])
        self.assertNotEqual(resolution["effective_lease_id"], lease["lease_id"])

    def test_serial_or_probe_mismatch_is_rejected_before_acquiring_lease_for_every_action(self) -> None:
        for action in ("device.probe", "device.execute_shell", "device.deploy_artifact", "device.collect_artifact"):
            with self.subTest(action=action):
                value = {**self._input(action), "logical_device_id": self.gateway.logical_device_id("another-phone")}
                with self.assertRaises(PlatformError) as raised:
                    self.router.execute(action, value, self.run)
                self.assertEqual(raised.exception.code, "DEVICE_TARGET_MISMATCH")
        self.assertEqual(self.gateway._read_registry()["leases"], {})
        self.dispatch.assert_not_called()

    def test_extra_serial_cannot_override_probe_device(self) -> None:
        value = {**self._input("device.execute_shell"), "device_serial": "another-phone"}
        with self.assertRaises(PlatformError) as raised:
            self.router.execute("device.execute_shell", value, self.run)
        self.assertEqual(raised.exception.code, "DEVICE_TARGET_MISMATCH")
        self.assertEqual(self.gateway._read_registry()["leases"], {})

    def test_discovery_with_only_bridge_context_needs_no_lease(self) -> None:
        run = {**self.run, "context": {"device_selector": {"bridge_id": self.bridge_id}}}
        result = self.router.execute("device.discover", {}, run)
        self.assertEqual(result["status"], "succeeded")
        self.assertIsNone(self.dispatch.call_args.args[0]["device_lease_id"])
        self.assertEqual(self.gateway._read_registry()["leases"], {})

    def test_local_device_actions_do_not_acquire_remote_lease(self) -> None:
        for action, method in (("device.probe", "local_probe"), ("device.execute_shell", "local_execute_shell"),
                               ("device.deploy_artifact", "local_deploy"), ("device.collect_artifact", "local_collect")):
            value = self._input(action)
            value.pop("bridge_id")
            value.pop("logical_device_id")
            def local_result(*_: object, **kwargs: object) -> dict:
                if action == "device.deploy_artifact":
                    return self._deployment({**kwargs, "device_serial": kwargs["serial"]})
                return {"status": "succeeded", "device": {"serial": self.serial}}
            with self.subTest(action=action), patch.object(self.gateway, method, side_effect=local_result) as execute:
                result = self.router.execute(action, value, self.run)
                self.assertEqual(result["status"], "succeeded")
                self.assertNotIn("lease_resolution", result)
                execute.assert_called_once()
        self.assertEqual(self.gateway._read_registry()["leases"], {})
        self.dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
