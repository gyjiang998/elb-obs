from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from backend.core import ArtifactStore, PlatformError, sha256_file
from backend.device_gateway import DeviceGateway
from backend.scenario_capture import CaptureCancelled, LocalDeviceClient, ScenarioCaptureManager
from backend.tests.test_scenario_capture import SimulatedDeviceClient


class ScenarioCaptureLocalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.gateway = DeviceGateway(workspace_root=self.root, state_root=self.root)
        self.manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), self.gateway)

    def plan(self) -> dict:
        return self.manager.create_plan({"template_id": "custom", "steps": [
            {"title": "prepare", "operation": "wait", "seconds": 0},
            {"title": "capture", "operation": "wait", "seconds": 0}],
            "capture": {"selected_step_ids": ["step_02"], "collectors": ["trace"]}})

    def test_catalog_resolves_hdc_without_probing_or_starting_it(self) -> None:
        with patch("backend.scenario_capture.sys.platform", "darwin"), \
                patch("backend.scenario_capture.resolve_hdc_binary", return_value=self.root / "hdc"), \
                patch.object(self.gateway, "local_discover") as discover, \
                patch.object(self.gateway, "local_probe") as probe, \
                patch("backend.device_gateway.subprocess.Popen") as process:
            catalog = self.manager.catalog()
        self.assertEqual(catalog["default_connection_mode"], "auto")
        self.assertEqual({mode["id"] for mode in catalog["connection_modes"]}, {"auto", "local", "bridge"})
        self.assertTrue(catalog["local_hdc"]["available"])
        discover.assert_not_called()
        probe.assert_not_called()
        process.assert_not_called()
        unavailable = PlatformError("DEVICE_HDC_UNAVAILABLE", "HDC is missing", status_code=503)
        with patch("backend.scenario_capture.sys.platform", "linux"), \
                patch("backend.scenario_capture.resolve_hdc_binary", side_effect=unavailable):
            catalog = self.manager.catalog()
        self.assertEqual(catalog["default_connection_mode"], "auto")
        self.assertFalse(catalog["local_hdc"]["available"])
        self.assertEqual(catalog["local_hdc"]["error_code"], "DEVICE_HDC_UNAVAILABLE")

    def test_local_discovery_needs_no_bridge_and_keeps_device_schema(self) -> None:
        devices = [{"serial": "phone", "connection_state": "connected"}]
        with patch.object(self.gateway, "local_discover", return_value={
                "schema_version": "device_discovery/v1", "hdc": {"available": True}, "devices": devices}) as discover, \
                patch.object(self.gateway, "_bridge_job") as bridge_job:
            result = self.manager.discover_devices(connection_mode="local")
        self.assertEqual(result["schema_version"], "scenario_capture_devices/v1")
        self.assertEqual(result["connection_mode"], "local")
        self.assertEqual(result["bridge_id"], "")
        self.assertEqual(result["devices"][0]["device_serial"], "phone")
        self.assertEqual(result["devices"][0]["connection_mode"], "local")
        self.assertEqual(result["devices"][0]["bridge_id"], "")
        self.assertEqual(result["diagnostics"][0]["status"], "ready")
        self.assertTrue(result["discovery_complete"])
        discover.assert_called_once_with(timeout_seconds=5)
        bridge_job.assert_not_called()

    def test_bridge_remains_default_and_modes_are_validated(self) -> None:
        plan = self.plan()
        for arguments in ({}, {"connection_mode": "invalid"}):
            with self.subTest(arguments=arguments), self.assertRaises(PlatformError):
                self.manager.execute(plan["plan_id"], device_serial="phone", **arguments)
            with self.assertRaises(PlatformError):
                self.manager.discover_devices(**arguments)
        with patch.object(self.gateway, "_bridge_job", return_value={"job_id": "bridge_job"}) as issue, \
                patch.object(self.gateway, "dispatch_bridge_job", return_value={"result": {"devices": []}}), \
                patch.object(self.gateway, "local_discover") as local:
            result = self.manager.discover_devices("bridge_test")
        self.assertEqual(result["connection_mode"], "bridge")
        self.assertEqual(result["bridge_id"], "bridge_test")
        issue.assert_called_once()
        local.assert_not_called()

    def test_local_runner_uses_gateway_and_retains_mode_and_hashes(self) -> None:
        simulated = SimulatedDeviceClient()

        def execute_shell(**kwargs):
            wrapped = kwargs["command"]
            marker = re.search(r"__SCENARIO_RC_[a-f0-9]+", wrapped)[0]
            command = wrapped[2:wrapped.index("\n); scenario_rc=")]
            output = simulated.shell(command, cleanup=kwargs["agent_run_id"].endswith(":cleanup"))
            return {"status": "succeeded", "execution": {"stdout": output + "\n" + marker + "0\n"}}

        def collect(**kwargs):
            destination = kwargs["destination"]
            simulated.collect(kwargs["remote_path"], destination)
            self.assertEqual(kwargs["max_bytes"], 2 * 1024 * 1024 * 1024)
            return {"status": "succeeded", "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}

        with patch.object(self.gateway, "local_execute_shell", side_effect=execute_shell) as shell, \
                patch.object(self.gateway, "local_collect", side_effect=collect), \
                patch.object(self.gateway, "create_lease") as lease, \
                patch.object(self.gateway, "_bridge_job") as bridge:
            run = self.manager.execute(self.plan()["plan_id"], connection_mode="local", device_serial="phone")
        self.assertEqual(run["status"], "succeeded", run)
        self.assertEqual(run["connection_mode"], "local")
        self.assertEqual(run["bridge_id"], "")
        self.assertEqual(run["manifest"]["device"]["executor"], "local_hdc")
        self.assertEqual(run["manifest"]["cleanup"]["status"], "completed")
        self.assertFalse(run["execution_provenance"]["host_shell"])
        directory = self.manager.root / "runs" / run["run_id"]
        retained = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(retained["connection_mode"], "local")
        self.assertEqual(retained["device"]["serial"], "phone")
        self.assertTrue(all(call.kwargs["serial"] == "phone" for call in shell.call_args_list))
        jobs = json.loads((directory / "device_jobs.json").read_text())["jobs"]
        self.assertTrue(jobs)
        self.assertTrue(all(job["connection_mode"] == "local" for job in jobs))
        self.assertTrue(any(job["cleanup"] for job in jobs))
        lease.assert_not_called()
        bridge.assert_not_called()

    def test_generated_cli_automates_connection_and_keeps_explicit_modes(self) -> None:
        script = self.manager.generate_script(self.plan()["plan_id"])
        namespace = {"__name__": "generated_capture_test"}
        exec(compile(script["content"], script["filename"], "exec"), namespace)
        rpc = Mock(side_effect=[{"plan_id": "plan_test"}, {"job_id": "job_test"}, {"status": "succeeded"}])
        namespace["rpc"] = rpc
        with patch.object(sys, "argv", ["capture_scenario.py", "--platform", "http://local", "--connection-mode", "local", "--device-serial", "phone"]), \
                patch("builtins.print"):
            namespace["main"]()
        self.assertEqual(rpc.call_args_list[1].args[2], {"connection_mode": "local", "bridge_id": "", "device_serial": "phone"})
        rpc.reset_mock()
        rpc.side_effect = [{"plan_id": "plan_test"}, {"job_id": "job_test"}, {"status": "succeeded"}]
        with patch.object(sys, "argv", ["capture_scenario.py", "--platform", "http://local"]), patch("builtins.print"):
            namespace["main"]()
        self.assertEqual(rpc.call_args_list[1].args[2], {"connection_mode": "auto", "bridge_id": "", "device_serial": ""})
        rpc.reset_mock()
        with patch.object(sys, "argv", ["capture_scenario.py", "--platform", "http://local", "--connection-mode", "bridge", "--device-serial", "phone"]), \
                patch("sys.stderr"), self.assertRaises(SystemExit) as error:
            namespace["main"]()
        self.assertEqual(error.exception.code, 2)
        rpc.assert_not_called()

    def make_stub_hdc(self) -> Path:
        """A real child process protocol stub; it never connects to a device."""
        path = self.root / "hdc-stub"
        path.write_text("#!" + sys.executable + "\n" + '''import pathlib, re, sys, time
args = sys.argv[1:]
if args == ["list", "targets"]:
    print("phone")
elif args == ["version"]:
    print("stub-hdc")
elif args[:2] == ["-t", "phone"]:
    args = args[2:]
    if args[0] == "shell":
        command = args[1]
        if "block_capture" in command:
            pathlib.Path(__file__).with_suffix(".started").write_text("started")
            time.sleep(30)
        if "missing_marker" in command:
            print("incomplete")
        else:
            marker = re.search(r"__SCENARIO_RC_[a-f0-9]+", command)[0]
            print("stub output")
            print(marker + ("7" if "exit 7" in command else "0"))
    elif args[:2] == ["file", "recv"]:
        destination = pathlib.Path(args[3])
        if args[2].endswith("large.bin"):
            with destination.open("wb") as output:
                output.truncate(33 * 1024 * 1024)
        else:
            destination.write_bytes(b"retained stub evidence")
    else:
        raise SystemExit(2)
else:
    raise SystemExit(3)
''')
        path.chmod(0o755)
        return path

    def client(self, cancel_event: threading.Event | None = None) -> LocalDeviceClient:
        return LocalDeviceClient(self.gateway, device_serial="phone", run_id="capture_" + "a" * 24,
            run_dir=self.root, agent_run_id="run_parent", cancel_event=cancel_event)

    def test_real_hdc_stub_observes_remote_exit_and_verified_collection(self) -> None:
        stub = self.make_stub_hdc()
        client = self.client()
        with patch.dict(os.environ, {"HETERO_HDC_BIN": str(stub)}):
            self.assertEqual(client.shell("echo value"), "stub output")
            with self.assertRaisesRegex(RuntimeError, "Remote shell exit 7"):
                client.shell("exit 7")
            with self.assertRaisesRegex(RuntimeError, "completion marker"):
                client.shell("missing_marker")
            destination = self.root / "artifact.bin"
            metadata = client.collect("/data/local/tmp/evidence.bin", destination)
        self.assertEqual(destination.read_bytes(), b"retained stub evidence")
        self.assertEqual(metadata["sha256"], hashlib.sha256(destination.read_bytes()).hexdigest())
        self.assertTrue(metadata["job_id"].startswith("localjob_"))

    def test_cancellation_stops_real_child_but_allows_owned_cleanup(self) -> None:
        stub = self.make_stub_hdc()
        cancel = threading.Event()
        client = self.client(cancel)
        started = stub.with_suffix(".started")
        with patch.dict(os.environ, {"HETERO_HDC_BIN": str(stub)}), ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(client.shell, "block_capture", 30)
            deadline = time.monotonic() + 4
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(started.exists(), "Stub HDC did not start")
            cancel.set()
            with self.assertRaises(CaptureCancelled):
                future.result(timeout=4)
            self.assertEqual(client.shell("cleanup_owned_collectors", cleanup=True), "stub output")
            metadata = client.collect("/data/local/tmp/evidence.bin", self.root / "after-cancel.bin")
            self.assertGreater(metadata["size_bytes"], 0)
            with self.assertRaises(CaptureCancelled):
                client.shell("next_action")
        self.assertFalse(self.gateway.local_executor._active_processes)
        self.assertIn(client.executor_run_id, self.gateway.local_executor._cancelled_runs)
        self.assertNotIn(client.cleanup_run_id, self.gateway.local_executor._cancelled_runs)
        jobs = json.loads((self.root / "device_jobs.json").read_text())["jobs"]
        self.assertEqual([job["status"] for job in jobs], ["cancelled", "succeeded", "succeeded"])
        self.assertEqual([job["cleanup"] for job in jobs], [False, True, True])

    def test_collection_rejects_mismatched_hash(self) -> None:
        destination = self.root / "invalid.bin"
        destination.write_bytes(b"actual bytes")
        with patch.object(self.gateway, "local_collect", return_value={"status": "succeeded", "sha256": "0" * 64}):
            with self.assertRaisesRegex(RuntimeError, "reported hash"):
                self.client().collect("/data/local/tmp/evidence.bin", destination)

    def test_larger_scenario_collection_keeps_gateway_default_limit(self) -> None:
        stub = self.make_stub_hdc()
        destination = self.root / "large.bin"
        with patch.dict(os.environ, {"HETERO_HDC_BIN": str(stub)}):
            with self.assertRaises(PlatformError) as error:
                self.gateway.local_collect(serial="phone", remote_path="/data/local/tmp/large.bin",
                    destination=destination, timeout_seconds=10)
            self.assertEqual(error.exception.code, "DEVICE_COLLECT_TOO_LARGE")
            self.assertFalse(destination.exists())
            result = self.client().collect("/data/local/tmp/large.bin", destination)
        self.assertEqual(destination.stat().st_size, 33 * 1024 * 1024)
        self.assertEqual(result["size_bytes"], destination.stat().st_size)
        self.assertEqual(result["sha256"], sha256_file(destination))

    def test_collection_cap_rejects_invalid_limits_before_starting_hdc(self) -> None:
        with patch.object(self.gateway.local_executor, "_ensure_connected") as connect:
            for maximum in (False, 0, -1, 2.5, 2 * 1024 * 1024 * 1024 + 1):
                with self.subTest(maximum=maximum), self.assertRaises(PlatformError) as error:
                    self.gateway.local_collect(serial="phone", remote_path="/data/local/tmp/evidence.bin",
                        destination=self.root / "artifact.bin", timeout_seconds=10, max_bytes=maximum)
                self.assertEqual(error.exception.code, "DEVICE_COLLECT_LIMIT_INVALID")
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
