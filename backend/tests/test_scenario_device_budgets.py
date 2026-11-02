"""Operation budgets and audit evidence, with protocol doubles only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from backend.core import ArtifactStore, PlatformError
from backend.device_gateway import DeviceGateway, LocalHdcDeviceExecutor
from backend.scenario_capture import (
    CaptureCancelled, GatewayDeviceClient, LocalDeviceClient, ScenarioCaptureManager,
    SCENARIO_BRIDGE_RESULT_GRACE_SECONDS,
)


class ScenarioDeviceBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def bridge_client(self, outcome):
        self.calls = []

        def dispatch(job, **kwargs):
            self.calls.append({"job": job, **kwargs})
            if isinstance(outcome, Exception):
                raise outcome
            return {"result": outcome}

        gateway = SimpleNamespace(
            logical_device_id=lambda serial: "device_phone",
            create_lease=lambda **kwargs: {"lease_id": "lease"},
            ensure_active_lease=lambda *args, **kwargs: {"effective_lease_id": "lease"},
            _bridge_job=lambda **kwargs: {"job_id": "devjob_test", **kwargs},
            dispatch_bridge_job=dispatch,
        )
        return GatewayDeviceClient(gateway, state_root=self.root, project_id="test", bridge_id="bridge",
            device_serial="phone", run_id="capture_" + "a" * 24, run_dir=self.root)

    def test_result_grace_does_not_extend_device_budget(self):
        client = self.bridge_client({"status": "succeeded"})
        client.rpc("device.collect_artifact", {}, timeout=8)
        call = self.calls[0]
        self.assertEqual(call["job"]["timeout_seconds"], 8)
        self.assertEqual(call["job"]["payload"]["timeout_seconds"], 8)
        self.assertEqual(call["wait_seconds"], 8 + SCENARIO_BRIDGE_RESULT_GRACE_SECONDS)
        record = json.loads((self.root / "device_jobs.json").read_text())["jobs"][0]
        self.assertEqual(record["status"], "succeeded")
        self.assertGreaterEqual(record["duration_ms"], 0)
        self.assertEqual(record["timeout_seconds"], 8)

    def test_remote_timeout_and_missing_result_remain_distinct(self):
        for outcome, expected in (({"status": "timed_out"}, "timed_out"),
                (PlatformError("BRIDGE_JOB_TIMEOUT", "unknown remote state", status_code=504), "state_unknown"),
                (RuntimeError("response transport lost"), "state_unknown")):
            with self.subTest(expected=expected):
                client = self.bridge_client(outcome)
                with self.assertRaises((RuntimeError, PlatformError)):
                    client.rpc("device.execute_shell", {}, timeout=2)
                record = json.loads((self.root / "device_jobs.json").read_text())["jobs"][0]
                self.assertEqual(record["status"], expected)
                self.assertGreaterEqual(record["duration_ms"], 0)
                if isinstance(outcome, PlatformError):
                    self.assertEqual(record["error_code"], "BRIDGE_JOB_TIMEOUT")

    def local_operation(self, executor, action):
        if action == "shell":
            return executor.execute_shell(serial="phone", command="echo fixture", timeout_seconds=10, agent_run_id="capture")
        return executor.collect(serial="phone", remote_path="/data/local/tmp/layout.json",
            destination=self.root / "layout.json", timeout_seconds=10, agent_run_id="capture")

    @staticmethod
    def hdc_result(**overrides):
        return {"return_code": 0, "stdout": "phone\n", "stderr": "", "timed_out": False, "cancelled": False, **overrides}

    def test_targets_check_and_command_share_one_budget(self):
        for action in ("shell", "collect"):
            with self.subTest(action=action):
                executor, now, calls = LocalHdcDeviceExecutor(self.root), [100.0], []

                def tracked(hdc, command, **kwargs):
                    calls.append((command, kwargs))
                    if command == ("list", "targets"):
                        now[0] += 7
                    elif command[:2] == ("file", "recv"):
                        Path(command[-1]).write_text("fixture")
                    return self.hdc_result()

                with patch("backend.device_gateway.resolve_hdc_binary", return_value=self.root / "stub"), \
                        patch("backend.device_gateway.time.monotonic", side_effect=lambda: now[0]), \
                        patch.object(executor, "_run_hdc_tracked", side_effect=tracked):
                    result = self.local_operation(executor, action)
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(calls[0][1]["timeout_seconds"], 10)
                self.assertEqual(calls[1][1]["timeout_seconds"], 3)
                self.assertEqual([kwargs["agent_run_id"] for _, kwargs in calls], ["capture", "capture"])

    def test_expired_budget_after_targets_does_not_launch_device_command(self):
        for action in ("shell", "collect"):
            with self.subTest(action=action):
                executor, now = LocalHdcDeviceExecutor(self.root), [100.0]

                def targets(*args, **kwargs):
                    now[0] += 10.1
                    return self.hdc_result()

                with patch("backend.device_gateway.resolve_hdc_binary", return_value=self.root / "stub"), \
                        patch("backend.device_gateway.time.monotonic", side_effect=lambda: now[0]), \
                        patch.object(executor, "_run_hdc_tracked", side_effect=targets) as execute:
                    with self.assertRaises(PlatformError) as error:
                        self.local_operation(executor, action)
                self.assertEqual(error.exception.code, "DEVICE_COMMAND_TIMEOUT")
                self.assertEqual(execute.call_count, 1)

    def test_targets_timeout_and_cancellation_do_not_masquerade_as_disconnect(self):
        for result, code in ((self.hdc_result(timed_out=True), "DEVICE_COMMAND_TIMEOUT"),
                (self.hdc_result(cancelled=True), "DEVICE_COMMAND_CANCELLED")):
            for action in ("shell", "collect"):
                with self.subTest(code=code, action=action):
                    executor = LocalHdcDeviceExecutor(self.root)
                    with patch("backend.device_gateway.resolve_hdc_binary", return_value=self.root / "stub"), \
                            patch.object(executor, "_run_hdc_tracked", return_value=result) as execute:
                        with self.assertRaises(PlatformError) as error:
                            self.local_operation(executor, action)
                    self.assertEqual(error.exception.code, code)
                    self.assertEqual(execute.call_count, 1)

    def test_partial_file_after_timed_out_receive_is_not_success(self):
        executor = LocalHdcDeviceExecutor(self.root)
        (self.root / "layout.json").write_text("partial")
        with patch.object(executor, "_ensure_connected", return_value=self.root / "stub"), \
                patch.object(executor, "_run_hdc_tracked", return_value=self.hdc_result(timed_out=True)):
            result = self.local_operation(executor, "collect")
        self.assertEqual(result["status"], "timed_out")
        self.assertNotIn("sha256", result)

    @unittest.skipUnless(os.name == "posix", "local executable fixture uses a POSIX shebang")
    def test_cancel_interrupts_targets_preflight_and_never_starts_shell(self):
        stub = self.root / "hdc-fixture"
        stub.write_text("#!" + sys.executable + "\n" + """import pathlib, sys, time
root = pathlib.Path(__file__).parent
if sys.argv[1:] == ['list', 'targets']:
    (root / 'targets.started').write_text('fixture')
    time.sleep(20)
    print('phone')
else:
    (root / 'unexpected-command').write_text(repr(sys.argv))
""")
        stub.chmod(0o755)
        gateway, flag = DeviceGateway(workspace_root=self.root, state_root=self.root), threading.Event()
        client = LocalDeviceClient(gateway, device_serial="phone", run_id="capture_fixture", run_dir=self.root, cancel_event=flag)
        with patch.dict(os.environ, {"HETERO_HDC_BIN": str(stub)}), ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(client.shell, "should_not_execute", 15)
            deadline = time.monotonic() + 3
            while not (self.root / "targets.started").exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue((self.root / "targets.started").exists())
            flag.set()
            with self.assertRaises(CaptureCancelled):
                pending.result(timeout=3)
        self.assertFalse((self.root / "unexpected-command").exists())
        self.assertFalse(gateway.local_executor._active_processes)
        self.assertEqual(client.jobs[0]["status"], "cancelled")

    def test_preparation_progress_keeps_run_fields_and_does_not_stick_afterwards(self):
        manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), DeviceGateway(workspace_root=self.root, state_root=self.root))
        plan = manager.create_plan({"template_id": "custom", "steps": [{"title": "capture", "operation": "wait", "seconds": 0}],
            "capture": {"selected_step_ids": ["step_01"], "collectors": ["trace"]}})
        updates = []

        def runner(plan, client, directory, run_id, update, *, terminal_run_ids):
            self.assertEqual(terminal_run_ids, set())
            manifest = {"status": "running", "files": [], "steps": [{"step_id": "step_01", "status": "preparing",
                "screen_preparation_timing": {"phase": "wake", "message": "正在唤醒设备"}}]}
            update(manifest)
            manifest = json.loads(json.dumps(manifest))
            manifest["steps"][0]["screen_evidence_timing"] = {"status": "running", "phase": "evidence", "message": "正在保留失败截图"}
            update(manifest)
            manifest = json.loads(json.dumps(manifest))
            manifest["status"], manifest["steps"][0]["status"] = "succeeded", "succeeded"
            return manifest

        with patch("backend.scenario_capture.execute_plan", side_effect=runner):
            run = manager.execute(plan["plan_id"], device_serial="phone", connection_mode="local", device_client=object(),
                progress=lambda value: updates.append(json.loads(json.dumps(value))))
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(updates[0]["message"], "正在唤醒设备")
        self.assertEqual(updates[0]["phase"], "wake")
        self.assertEqual(updates[1]["phase"], "evidence")
        self.assertEqual(updates[1]["message"], "正在保留失败截图")
        for key in ("run_id", "plan_id", "device", "execution_provenance", "manifest"):
            self.assertIn(key, updates[0])
        self.assertNotIn("message", updates[-1])
        self.assertNotIn("phase", updates[-1])


if __name__ == "__main__":
    unittest.main()
