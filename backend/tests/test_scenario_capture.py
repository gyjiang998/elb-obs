from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from backend.core import ArtifactStore, PlatformError
from backend.scenario_capture import CaptureCancelled, GatewayDeviceClient, ScenarioCaptureManager
from tools.scenario_capture.runner import (STEP_SHELL_CLEANUP_ALLOWANCE_SECONDS,
                                           STEP_SHELL_CLEANUP_BOUND_SECONDS, TRACE_FINISH_MAX_TICKS,
                                           build_step_script,
                                           stage_script, validate_pmu, validate_trace)


def trace_fixture(number: int = 2, slug: str = "action_02") -> str:
    return "\n".join([
        f"app-100 [000] .... 1.000000: tracing_mark_write: B|100|GALLERY_STEP_{number:02d}_{slug}",
        f"app-100 [000] .... 1.001000: tracing_mark_write: B|100|GALLERY_ACTION_{number:02d}",
        "app-100 [000] .... 1.002000: sched_switch: prev_pid=100 next_pid=101",
        "app-100 [000] .... 1.010000: tracing_mark_write: E|100",
        f"app-100 [000] .... 1.011000: tracing_mark_write: B|100|GALLERY_HOLD_{number:02d}",
        "app-100 [000] .... 2.011000: tracing_mark_write: E|100",
        "app-100 [000] .... 2.012000: tracing_mark_write: E|100",
    ]) + "\n"


class SimulatedDeviceClient:
    """A device protocol double; no real host HDC or phone is consulted."""
    def __init__(self, *, fail_step: bool = False) -> None:
        self.commands: list[str] = []
        self.files: dict[str, bytes] = {}
        self.executed: list[str] = []
        self.fail_step = fail_step
        self.collector_session: dict | None = None
        self.collector_session_events: list[dict] = []
        self.application_running = True

    def check_cancelled(self) -> None:
        pass

    def shell(self, command: str, timeout: int = 40, *, cleanup: bool = False) -> str:
        self.commands.append(command)
        if command.startswith("# HETERO_COLLECTOR_SESSION "):
            action = command.splitlines()[0].split()[-1]
            fields = {}
            for line in command.splitlines()[1:]:
                if re.match(r"^session_(lock|token|run|root)=", line):
                    name, value = line.split("=", 1)
                    fields[name] = shlex.split(value)[0]
            self.collector_session_events.append({"action": action, "cleanup": cleanup, **fields})
            if action == "inspect":
                owner = (self.collector_session or {}).get("session_run", "")
                return "HETERO_COLLECTOR_OWNER|" + owner + "\n"
            if action == "acquire":
                if self.collector_session is not None:
                    return "HETERO_COLLECTOR_SESSION|busy|active_lease"
                self.collector_session = fields
                return "HETERO_COLLECTOR_SESSION|acquired"
            if (self.collector_session is None or
                    self.collector_session.get("session_token") != fields.get("session_token")):
                return ("HETERO_COLLECTOR_SESSION|unowned|session_ownership_lost" if action in {"recover", "release"}
                        else "HETERO_COLLECTOR_SESSION|busy|session_ownership_lost")
            if action == "release":
                self.collector_session = None
                return "HETERO_COLLECTOR_SESSION|released"
            if action in {"refresh", "recover"}:
                return "HETERO_COLLECTOR_SESSION|" + {"refresh": "refreshed", "recover": "recovered"}[action]
            raise AssertionError("Unexpected collector session operation: " + action)
        if command.startswith("printf %s "):
            args = shlex.split(command)
            if args[-2] == ">":
                self.files[args[-1]] = args[2].encode()
            else:
                self.files[args[-1]] += args[2].encode()
        if command.startswith("uitest dumpLayout"):
            remote = shlex.split(command)[-1]
            self.files[remote] = json.dumps({"attributes": {"bounds": "[0,0][1280,2832]"}, "children": [
                {"attributes": {"id": "SCBDesktop_Flex_Desktop", "bundleName": "com.ohos.sceneboard",
                                "type": "Column", "bounds": "[0,0][1280,2832]", "visible": "true"},
                 "children": []}]}).encode()
        if command.startswith("for root in"):
            return "/sys/kernel/tracing=0"
        if command == "hitrace -l":
            return "sched - Scheduling\napp - App\nace - ArkUI\n"
        if command.startswith("aa force-stop "):
            self.application_running = False
        if command.startswith("aa start "):
            self.application_running = True
        if command.startswith("pidof "):
            return "123" if self.application_running else ""
        if command.startswith("sh "):
            remote = shlex.split(command)[1]
            self.executed.append(remote)
            root = str(Path(remote).parent)
            source = self.files[remote].decode()
            if "command_action LAUNCH aa start" in source:
                self.application_running = True
            for name, data in {"status.txt": "0", "actions.log": "No Error\n", "clock_before.txt": "1000\n1.0", "clock_after.txt": "1001\n2.0"}.items():
                self.files[root + "/" + name] = data.encode()
            self.files[root + "/cleanup_phase.txt"] = b"cleanup_complete\n"
            self.files[root + "/cleanup_events.log"] = b""
            if "hitrace --trace_begin" in source:
                self.files[root + "/trace.ftrace"] = trace_fixture().encode()
                self.files[root + "/trace_clock.txt"] = b"local [mono] global"
                self.files[root + "/buffer_stats.txt"] = b"overrun: 0\ncommit overrun: 0\n"
                self.files[root + "/hitrace_begin.log"] = b""
                self.files[root + "/hitrace_finish.log"] = b""
            if "pmu_state=unavailable" in source:
                self.files[root + "/pmu_status.txt"] = b"unavailable"
                self.files[root + "/pmu.log"] = b"Preinstalled sampler unavailable"
            if self.fail_step:
                self.files[root + "/status.txt"] = b"1"
                raise RuntimeError("simulated action failed")
        return ""

    def collect(self, remote: str, destination: Path, *, timeout: int = 600, cleanup: bool = True) -> dict:
        if remote not in self.files:
            raise FileNotFoundError(remote)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.files[remote])
        return {"job_id": "devjob_simulated"}


class ScenarioCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.store = ArtifactStore(self.root)
        self.manager = ScenarioCaptureManager(self.root, self.store, object())

    def custom_plan(self, collectors: list[str] | None = None) -> dict:
        return self.manager.create_plan({"template_id": "custom", "steps": [
            {"title": "准备", "operation": "wait", "seconds": 0},
            {"title": "选中操作", "operation": "wait", "seconds": 0},
            {"title": "之后不执行", "operation": "wait", "seconds": 0}],
            "capture": {"selected_step_ids": ["step_02"], "collectors": collectors or ["trace"]}})

    def test_gallery_template_and_selected_step_boundary(self) -> None:
        plan = self.manager.create_plan({})
        self.assertEqual(len(plan["steps"]), 17)
        self.assertEqual(plan["capture"]["selected_step_ids"], ["step_07"])
        self.assertEqual(plan["execution_step_ids"], [f"step_{i:02d}" for i in range(1, 8)])
        self.assertEqual(len(plan["preparation_step_ids"]), 6)
        self.assertEqual(plan["readiness"]["status"], "ready_for_device_preflight")
        self.assertEqual(len(plan["provenance"]["files"]), 4)

    def test_uncalibrated_city_selectors_block_only_when_reached(self) -> None:
        plan = self.manager.create_plan({"capture": {"selected_step_ids": ["step_15"]}})
        missing = {item.get("target") for item in plan["readiness"]["blockers"]}
        self.assertIn("first_city", missing)
        self.assertIn("city_page", missing)
        with self.assertRaises(PlatformError):
            self.manager.execute(plan["plan_id"], bridge_id="bridge_test", device_serial="phone")

    def test_natural_instructions_are_saved_for_agent_resolution(self) -> None:
        plan = self.manager.create_plan({"template_id": "custom", "natural_language_steps": "点击照片", "steps": ["点击照片"]})
        self.assertEqual(plan["steps"][0]["operation"], "unresolved")
        self.assertEqual(plan["readiness"]["status"], "needs_input")

    def test_already_prepared_skips_only_steps_before_first_selection(self) -> None:
        plan = self.manager.create_plan({"template_id": "custom", "steps": [
            {"title": "skip", "operation": "wait", "seconds": 0},
            {"title": "capture", "operation": "wait", "seconds": 0},
            {"title": "necessary intermediate action", "operation": "wait", "seconds": 0},
            {"title": "capture again", "operation": "wait", "seconds": 0},
        ], "capture": {"selected_step_ids": ["step_02", "step_04"], "preparation_mode": "already_prepared", "collectors": ["pmu"]}})
        self.assertEqual(plan["execution_step_ids"], ["step_02", "step_03", "step_04"])
        self.assertEqual(plan["preparation_step_ids"], ["step_03"])
        device = SimulatedDeviceClient()
        run = self.manager.execute(plan["plan_id"], bridge_id="bridge_test", device_serial="phone", device_client=device)
        self.assertEqual([step["ordinal"] for step in run["manifest"]["steps"]], [2, 3, 4])
        self.assertEqual(len(device.executed), 3)

    def test_rejects_executable_steps_invalid_collectors_and_bad_paths(self) -> None:
        invalid = [
            {"template_id": "custom", "steps": [{"title": "x", "operation": "wait", "shell": "rm -rf /"}]},
            {"capture": {"collectors": ["unknown"]}},
            {"capture": {"selected_step_ids": ["absent"]}},
            {"capture": {"pmu": {"binary_path": "/data/local/tmp/../../bin/sh"}}},
            {"profile": {"trace_categories": ["sched;echo x"]}},
            {"profile": {"screen_size": [True, 1000]}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(PlatformError):
                self.manager.create_plan(payload)

    def test_generated_python_embeds_plan_and_uses_registered_rpc(self) -> None:
        plan = self.custom_plan()
        script = self.manager.generate_script(plan["plan_id"])
        ast.parse(script["content"])
        self.assertIn("/v1/scenario-capture/plans", script["content"])
        self.assertIn("/v1/parallel-jobs/", script["content"])
        self.assertIn("选中操作", script["content"])
        self.assertNotIn("subprocess", script["content"])
        self.assertNotIn("import backend", script["content"])

    def test_windows_local_package_is_self_contained_and_authenticates_plan(self) -> None:
        plan = self.custom_plan(["trace", "pmu"])
        package = self.manager.generate_local_package(plan["plan_id"], device_serial="PHONE-01",
            bridge_id="bridge_windows", connection_mode="bridge")
        archive = self.root / package["filename"]
        archive.write_bytes(package["content"])
        extracted = self.root / "local-package"
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            self.assertIn("run-capture.ps1", names)
            self.assertIn("runtime/tools/scenario_capture/run_local_package.py", names)
            self.assertIn("runtime/tools/scenario_capture/thread_pmu_sampler.c", names)
            self.assertIn("pmu-sampler/manifest.json", names)
            bundle.extractall(extracted)
        from tools.scenario_capture.run_local_package import load_package
        _, metadata, embedded = load_package(extracted)
        self.assertEqual(metadata["selected_target"], {"connection_mode": "bridge",
            "bridge_id": "bridge_windows", "device_serial": "PHONE-01"})
        self.assertEqual(embedded["plan_id"], plan["plan_id"])
        isolated = subprocess.run([sys.executable, "-c",
            "from tools.scenario_capture.pmu_bundle import verify_bundle; import json; print(json.dumps(verify_bundle('pmu-sampler'), default=str))"],
            cwd=extracted, env={**os.environ, "PYTHONPATH": str(extracted / "runtime")},
            capture_output=True, text=True, timeout=20)
        self.assertEqual(isolated.returncode, 0, isolated.stderr)
        self.assertIn('"sampler_version": "1.2.0"', isolated.stdout)

    def test_windows_local_package_can_be_generated_without_online_device_selection(self) -> None:
        plan = self.custom_plan(["trace"])
        package = self.manager.generate_local_package(plan["plan_id"])
        self.assertEqual(package["package"]["selected_target"]["device_serial"], "")
        archive = self.root / package["filename"]
        archive.write_bytes(package["content"])
        with zipfile.ZipFile(archive) as bundle:
            readme = bundle.read("README.txt").decode("utf-8")
        self.assertIn("唯一在线 HDC 设备", readme)
        self.assertIn('-DeviceSerial "YOUR_DEVICE_SERIAL"', readme)
        with self.assertRaises(PlatformError) as caught:
            self.manager.generate_local_package(plan["plan_id"], device_serial="invalid serial")
        self.assertEqual(caught.exception.code, "LOCAL_CAPTURE_DEVICE_INVALID")

    def test_runner_executes_preparation_without_collectors_and_stops_at_selection(self) -> None:
        plan = self.custom_plan()
        device = SimulatedDeviceClient()
        run = self.manager.execute(plan["plan_id"], bridge_id="bridge_test", device_serial="phone", device_client=device)
        self.assertEqual(run["status"], "succeeded", run)
        self.assertEqual(len(device.executed), 2)
        self.assertNotIn("hitrace --trace_begin", device.files[device.executed[0]].decode())
        self.assertIn("hitrace --trace_begin", device.files[device.executed[1]].decode())
        manifest = run["manifest"]
        self.assertEqual([step["captured"] for step in manifest["steps"]], [False, True])
        self.assertEqual(manifest["cleanup"]["status"], "completed")
        trace = next(item for item in manifest["files"] if item["kind"] == "trace")
        self.assertEqual(trace["step_id"], "step_02")
        self.assertTrue((self.root / trace["managed_path"]).is_file())
        self.assertEqual(self.manager.get_run(run["run_id"])["manifest_artifact_id"], run["manifest_artifact_id"])

    def test_missing_pmu_retains_partial_manifest_and_other_collectors(self) -> None:
        plan = self.custom_plan(["trace", "pmu"])
        run = self.manager.execute(plan["plan_id"], bridge_id="bridge_test", device_serial="phone", device_client=SimulatedDeviceClient())
        self.assertEqual(run["status"], "partial", run)
        status = run["manifest"]["steps"][1]["collector_status"]
        self.assertEqual(status, {"trace": "collected", "pmu": "unavailable"})
        self.assertTrue(run["manifest_artifact_id"])

    def test_failed_action_still_collects_evidence_and_releases_owned_lock(self) -> None:
        device = SimulatedDeviceClient(fail_step=True)
        run = self.manager.execute(self.custom_plan()["plan_id"], bridge_id="bridge_test", device_serial="phone", device_client=device)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["message"], "simulated action failed")
        self.assertEqual(run["error"]["step_id"], "step_01")
        self.assertEqual(run["manifest"]["cleanup"]["status"], "completed")
        self.assertEqual(len(device.executed), 1)
        self.assertTrue(any(item["kind"] == "execution_status" for item in run["manifest"]["files"]))
        self.assertEqual(run["manifest"]["steps"][0]["device_script_cleanup"],
                         {"last_phase": "cleanup_complete", "events": "", "bounded": True})
        self.assertTrue(any("owner.pid" in command and "kill -TERM" in command for command in device.commands))

    def test_nonzero_device_status_fetches_cleanup_diagnostic_and_names_last_child(self) -> None:
        class CleanupIncompleteClient(SimulatedDeviceClient):
            def shell(self, command: str, timeout: int = 40, *, cleanup: bool = False) -> str:
                result = super().shell(command, timeout=timeout, cleanup=cleanup)
                if command.startswith("sh "):
                    root = str(Path(shlex.split(command)[1]).parent)
                    self.files[root + "/status.txt"] = b"1\n"
                    self.files[root + "/cleanup_phase.txt"] = b"cleanup_incomplete\n"
                    self.files[root + "/cleanup_events.log"] = b"trace_finisher TERM after 500 polls\ntrace_finisher KILL after TERM grace\n"
                return result

        run = self.manager.execute(self.custom_plan()["plan_id"], bridge_id="bridge_test",
                                   device_serial="phone", device_client=CleanupIncompleteClient())
        self.assertEqual(run["status"], "failed")
        self.assertIn("cleanup stopped at cleanup_incomplete", run["error"]["message"])
        self.assertIn("last cleanup event: trace_finisher KILL after TERM grace", run["error"]["message"])
        diagnostic = run["manifest"]["steps"][0]["device_script_cleanup"]
        self.assertEqual(diagnostic["last_phase"], "cleanup_incomplete")
        self.assertIn("trace_finisher KILL", diagnostic["events"])
        kinds = {item["kind"] for item in run["manifest"]["files"]}
        self.assertIn("collector_cleanup_phase", kinds)
        self.assertIn("collector_cleanup_log", kinds)

    def test_trace_and_pmu_quality_do_not_upgrade_bad_data(self) -> None:
        trace = self.root / "trace.ftrace"
        trace.write_text(trace_fixture())
        step = {"ordinal": 2, "slug": "action_02"}
        self.assertTrue(validate_trace(trace, step, 1, "overrun: 0")["valid"])
        self.assertFalse(validate_trace(trace, step, 1, "overrun: 2")["valid"])
        trace.write_text(trace_fixture().replace("E|100", "E|999"))
        self.assertFalse(validate_trace(trace, step, 1)["valid"])
        (self.root / "pmu.json").write_text('{"sample_count":1}')
        (self.root / "pmu.csv").write_text("read_error,enabled_ns,running_ns\n1,100,0\n")
        self.assertFalse(validate_pmu(self.root)["valid"])

    def test_staged_large_script_never_exceeds_bridge_command_limit(self) -> None:
        client = SimulatedDeviceClient()
        content = "echo '你好';\n" * 3000
        remote = "/data/local/tmp/test/capture.sh"
        stage_script(client, remote, content)
        self.assertEqual(client.files[remote].decode(), content)
        self.assertTrue(all(len(command.encode()) < 15 * 1024 for command in client.commands))

    def test_staging_packs_escaped_utf8_and_reuses_existing_directory(self) -> None:
        client = SimulatedDeviceClient()
        remote = "/data/local/tmp/run/capture.sh"
        content = "# 引号 ' $() `backticks` 🙂\n" * 1000
        stage_script(client, remote, content, directory_ready=True)
        self.assertEqual(client.files[remote].decode(), content)
        self.assertTrue(all(command.startswith("printf %s ") for command in client.commands))
        self.assertTrue(all(len(command.encode("utf-8")) <= 12 * 1024 for command in client.commands))
        self.assertLess(len(client.commands), (len(content) + 2999) // 3000)

    def test_staging_empty_script_preserves_posix_remote_path(self) -> None:
        client = SimulatedDeviceClient()
        stage_script(client, "/data/local/tmp/run/capture.sh", "")
        self.assertEqual(client.commands[0], "mkdir -p /data/local/tmp/run")
        self.assertEqual(client.files["/data/local/tmp/run/capture.sh"], b"")

    def test_pmu_partial_window_and_bad_rows_never_claim_full_capture(self) -> None:
        metadata = {"sample_count": 3, "clock": "CLOCK_MONOTONIC", "target_tid": 10}
        (self.root / "pmu.json").write_text(json.dumps(metadata))
        header = "read_begin_ns,read_end_ns,read_error,enabled_ns,running_ns\n"
        rows = ["900000000,900000100,0,100,100", "1500000000,1500000100,0,200,200", "2100000000,2100000100,0,300,300"]
        (self.root / "pmu.csv").write_text(header + "\n".join(rows))
        full = validate_pmu(self.root, target_tid=10, step_window={"start_s": 1.0, "end_s": 2.0})
        self.assertTrue(full["valid"])
        self.assertTrue(full["quality_complete"])
        partial = validate_pmu(self.root, target_tid=10, step_window={"start_s": 1.0, "end_s": 3.0})
        self.assertTrue(partial["valid"])
        self.assertFalse(partial["quality_complete"])
        self.assertEqual(partial["coverage"]["status"], "partial")
        rows[1] = rows[1].replace(",0,200,200", ",1,200,200")
        (self.root / "pmu.csv").write_text(header + "\n".join(rows))
        partial = validate_pmu(self.root, target_tid=10)
        self.assertTrue(partial["valid"])
        self.assertFalse(partial["quality_complete"])
        self.assertEqual(partial["read_error_rows"], 1)
        self.assertFalse(validate_pmu(self.root, target_tid=99)["valid"])

    def test_composite_shell_runs_background_collectors_and_cleans_on_failure(self) -> None:
        # Execute generated shell against local stub tools, never against a phone.
        plan = self.custom_plan(["trace", "hilog", "pmu"])
        step = plan["steps"][1]
        profile = {**plan["profile"], "guard_seconds": 0, "hold_seconds": 0}
        bins, out, tracing = self.root / "bin", self.root / "out", self.root / "tracing"
        bins.mkdir(); out.mkdir(); tracing.mkdir()
        (tracing / "tracing_on").write_text("0")
        (tracing / "trace_clock").write_text("[mono]")
        (tracing / "trace_marker").write_text("")
        timeline = self.root / "timeline"
        for name, source in {
            "hitrace": '#!/bin/sh\necho "$1" >> "$TIMELINE"\nif [ "$1" = --trace_begin ]; then echo 1 > "$TRACING/tracing_on"; else echo 0 > "$TRACING/tracing_on"; echo trace > "$3"; fi\n',
            "hilog": '#!/bin/sh\ntrap "exit 0" TERM INT\nwhile :; do echo event; sleep .01; done\n',
            "fake_action": '#!/bin/sh\necho action >> "$TIMELINE"\nexit 3\n',
        }.items():
            path = bins / name
            path.write_text(source)
            path.chmod(0o755)
        script = build_step_script(step, [{"name": "ACTION", "command": ["fake_action"]}], profile, plan["capture"], str(out), str(tracing), "123")
        path = self.root / "capture.sh"
        path.write_text(script)
        env = {**os.environ, "PATH": str(bins) + os.pathsep + os.environ["PATH"], "TIMELINE": str(timeline), "TRACING": str(tracing)}
        result = subprocess.run(["/bin/sh", str(path)], env=env, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(timeline.read_text().splitlines(), ["--trace_begin", "action", "--trace_finish"])
        self.assertEqual((tracing / "tracing_on").read_text().strip(), "0")
        self.assertFalse((out / "owner.pid").exists())
        self.assertEqual((out / "pmu_status.txt").read_text().strip(), "unavailable")
        self.assertEqual((out / "hilog_status.txt").read_text().strip(), "stopped")

    def test_step_shell_has_bounded_collector_reaping_and_smaller_host_allowance(self) -> None:
        plan = self.custom_plan(["trace", "hilog", "pmu"])
        step = plan["steps"][1]
        # Trace flush time is independent of the action deadline and scales
        # with buffer size without giving a small stuck finisher 25 seconds.
        plan["profile"]["step_timeout_seconds"] = 10
        plan["profile"]["trace_buffer_kb"] = 1024
        source = build_step_script(step, [{"name": "ACTION", "command": ["true"]}], plan["profile"],
                                   plan["capture"], "/data/local/tmp/run/step", "/sys/kernel/tracing", "123")
        small_wait = int(re.search(r'wait_child_bounded "\$finisher" (\d+) trace_finisher', source).group(1))
        plan["profile"]["trace_buffer_kb"] = 1048576
        large_source = build_step_script(step, [{"name": "ACTION", "command": ["true"]}], plan["profile"],
                                         plan["capture"], "/data/local/tmp/run/step", "/sys/kernel/tracing", "123")
        large_wait = int(re.search(r'wait_child_bounded "\$finisher" (\d+) trace_finisher', large_source).group(1))
        self.assertLess(small_wait, large_wait)
        self.assertLessEqual(large_wait, TRACE_FINISH_MAX_TICKS)
        self.assertIn('stop_child_bounded "$sampler"', source)
        self.assertIn('stop_child_bounded "$logger"', source)
        self.assertIn('stop_child_bounded "$trace_heartbeat"', source)
        self.assertIn('stop_child_bounded "$action_child"', source)
        self.assertNotIn('wait "$finisher"', source)
        self.assertNotIn('wait "$sampler"', source)
        self.assertNotIn('wait "$logger"', source)
        self.assertNotIn('wait "$trace_heartbeat"', source)
        self.assertNotIn('wait "$pmu_probe" 2>/dev/null; pmu_caps_rc=$?', source)
        self.assertIn('cleanup_incomplete', source)

        class TimeoutRecordingClient(SimulatedDeviceClient):
            def __init__(self) -> None:
                super().__init__()
                self.shell_timeouts: list[tuple[str, int]] = []

            def shell(self, command: str, timeout: int = 40, *, cleanup: bool = False) -> str:
                self.shell_timeouts.append((command, timeout))
                return super().shell(command, timeout=timeout, cleanup=cleanup)

        # The saved plan retains its default 120 second action timeout.
        plan["profile"]["step_timeout_seconds"] = 120
        device = TimeoutRecordingClient()
        run = self.manager.execute(plan["plan_id"], bridge_id="bridge_test", device_serial="phone",
                                   device_client=device)
        self.assertIn(run["status"], {"succeeded", "partial"})
        step_timeout = next(timeout for command, timeout in device.shell_timeouts if command.startswith("sh "))
        self.assertEqual(step_timeout, plan["profile"]["step_timeout_seconds"] +
                         STEP_SHELL_CLEANUP_ALLOWANCE_SECONDS)
        self.assertEqual(step_timeout, 165)
        self.assertLessEqual(STEP_SHELL_CLEANUP_BOUND_SECONDS, 40)
        self.assertLess(STEP_SHELL_CLEANUP_BOUND_SECONDS, STEP_SHELL_CLEANUP_ALLOWANCE_SECONDS)

    def test_gateway_client_checks_remote_exit_and_recovers_verified_upload(self) -> None:
        outer = self

        class GatewayDouble:
            def __init__(self) -> None:
                self.payloads = []

            def logical_device_id(self, serial):
                return "device_" + serial

            def create_lease(self, **kwargs):
                return {"lease_id": "lease_test"}

            def ensure_active_lease(self, *args, **kwargs):
                return {"effective_lease_id": "lease_test"}

            def _bridge_job(self, **kwargs):
                self.payloads.append(kwargs)
                return {**kwargs, "job_id": "devjob_" + "a" * 24}

            def dispatch_bridge_job(self, job, **kwargs):
                if job["action"] == "device.collect_artifact":
                    data = b"retained trace"
                    digest = hashlib.sha256(data).hexdigest()
                    path = outer.root / "device-gateway" / "bridge-collected" / f"{job['job_id']}-{digest[:16]}.bin"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    return {"result": {"status": "succeeded", "sha256": digest}}
                command = job["payload"]["command"]
                marker = re.search(r"__SCENARIO_RC_[a-f0-9]+", command)[0]
                return {"result": {"status": "succeeded", "execution": {"return_code": 0, "stdout": "value\n" + marker + ("7\n" if "exit 7" in command else "0\n")}}}

        gateway = GatewayDouble()
        client = GatewayDeviceClient(gateway, state_root=self.root, project_id="local-default", bridge_id="bridge_test", device_serial="phone", run_id="capture_" + "b" * 24, run_dir=self.root)
        self.assertEqual(client.shell("echo value"), "value")
        with self.assertRaisesRegex(RuntimeError, "exit 7"):
            client.shell("exit 7")
        path = self.root / "trace.bin"
        client.collect("/data/local/tmp/trace.ftrace", path)
        self.assertEqual(path.read_bytes(), b"retained trace")
        self.assertTrue(all(item["payload"]["device_serial"] == "phone" for item in gateway.payloads))
        self.assertTrue(all(item["bridge_id"] == "bridge_test" for item in gateway.payloads))

    def test_gateway_cancellation_interrupts_waiting_job(self) -> None:
        flag = threading.Event()
        cancelled = threading.Event()

        class WaitingGateway:
            def logical_device_id(self, serial):
                return "device_phone"

            def create_lease(self, **kwargs):
                return {"lease_id": "lease_test"}

            def ensure_active_lease(self, *args, **kwargs):
                return {"effective_lease_id": "lease_test"}

            def _bridge_job(self, **kwargs):
                return {**kwargs, "job_id": "devjob_" + "c" * 24}

            def dispatch_bridge_job(self, job, **kwargs):
                flag.set()
                if not cancelled.wait(2):
                    raise AssertionError("Cancellation did not reach gateway")
                return {"result": {"status": "cancelled"}}

            def cancel_bridge_job(self, **kwargs):
                cancelled.set()

        client = GatewayDeviceClient(WaitingGateway(), state_root=self.root, project_id="local-default", bridge_id="bridge_test", device_serial="phone", run_id="capture_" + "c" * 24, run_dir=self.root, cancel_event=flag)
        with self.assertRaises(CaptureCancelled):
            client.shell("sleep 200")
        self.assertTrue(cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
