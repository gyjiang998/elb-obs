"""Exercise shared PMU scripts for non-Gallery apps without a phone or HDC."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from backend.core import ArtifactStore
from backend.scenario_capture import ScenarioCaptureManager
from backend.tests.test_scenario_capture import SimulatedDeviceClient
from tools.scenario_capture.collector_cleanup import REMOTE_LOCK
from tools.scenario_capture.runner import execute_plan


class ScriptDevice(SimulatedDeviceClient):
    """Run only generated capture shell in a temp directory, with a stub sampler."""

    def __init__(self, root: Path, bundle: str, pid_reply: str) -> None:
        super().__init__()
        self.root, self.bundle, self.pid_reply = root, bundle, pid_reply
        self.scripts: list[str] = []
        self.proc = root / "proc"
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        (self.proc / "sys/kernel/random/boot_id").write_text("qa-device-boot\n")
        (self.proc / "uptime").write_text("1000.00 0.00\n")
        self.session_lock = root / "session.lock"
        self.session_lock.mkdir()
        # macOS has no Linux procfs. Expose stable fixture start ticks only for
        # live children; the generated registration code still runs unchanged.
        self.bins = root / "bin"
        self.bins.mkdir()
        sed = self.bins / "sed"
        sed.write_text("#!/bin/sh\n" + f"fixture_proc={shlex.quote(str(self.proc))}\n" + r'''
case "${2:-}" in "$fixture_proc"/[0-9]*/stat)
  child=${2%/stat}; child=${child##*/}
  kill -0 "$child" 2>/dev/null || exit 1
  printf 'S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1234\n';;
*) exec /usr/bin/sed "$@";;
esac
''')
        sed.chmod(0o755)

    def shell(self, command: str, timeout: int = 40, *, cleanup: bool = False) -> str:
        if command.startswith("pidof "):
            self.commands.append(command)
            return self.pid_reply
        if command.startswith("sh "):
            self.commands.append(command)
            remote_script = shlex.split(command)[1]
            remote_dir = str(Path(remote_script).parent)
            self.executed.append(remote_script)
            source = self.files[remote_script].decode()
            self.scripts.append(source)
            local_dir = self.root / ("script_" + str(len(self.scripts)))
            local_dir.mkdir(parents=True)
            local_script = local_dir / "capture.sh"
            # The source remains unchanged in the retained remote artifact.
            # Device paths are mapped to this isolated filesystem, including
            # the token acquired by the actual runner before script dispatch.
            if self.collector_session is None:
                raise AssertionError("Capture script dispatched without an owned collector session")
            (self.session_lock / "token").write_text(self.collector_session["session_token"])
            (self.session_lock / "boot_id").write_text("qa-device-boot\n")
            local_script.write_text(source.replace(remote_dir, str(local_dir))
                                    .replace(REMOTE_LOCK, str(self.session_lock))
                                    .replace("/proc/", str(self.proc) + "/"))
            environment = {**os.environ, "PATH": str(self.bins) + os.pathsep + os.environ["PATH"]}
            result = subprocess.run(["/bin/sh", str(local_script)], capture_output=True,
                                    text=True, timeout=15, env=environment)
            if result.returncode:
                raise RuntimeError(result.stderr or result.stdout)
            for path in local_dir.iterdir():
                if path.is_file():
                    self.files[remote_dir + "/" + path.name] = path.read_bytes()
            return result.stdout
        result = super().shell(command, timeout=timeout, cleanup=cleanup)
        if command.startswith("uitest dumpLayout"):
            remote = shlex.split(command)[-1]
            # Before a cold launch the device is on the desktop. Afterwards
            # the requested application, not Gallery, is visible.
            if self.pid_reply.strip() or self.executed:
                self.files[remote] = json.dumps({"attributes": {
                    "bounds": "[0,0][1280,2832]"}, "children": [{"attributes": {
                        "bounds": "[0,0][1280,2832]", "bundleName": self.bundle,
                        "id": "custom_app_root", "visible": "true"}, "children": []}]}).encode()
        return result


class CrossApplicationPmuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), object())
        self.calls = self.root / "sampler_calls.jsonl"
        self.sampler = self.root / "shared_thread_sampler"
        self.sampler.write_text(f"#!{sys.executable}\n" + r'''
import csv as csv_module, json, pathlib, signal, sys, time
calls = pathlib.Path(__file__).with_name("sampler_calls.jsonl")
with calls.open("a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:] == ["--capabilities"]:
    print(json.dumps({"schema_version": "gallery_thread_pmu_capabilities/v1",
                      "period_us_min": 10, "period_us_max": 10000,
                      "sampler_version": "1.2.0", "event_groups": ["auto", "all", "cache"]}))
    sys.exit(0)
if sys.argv[1:] == ["--clock"]:
    print("{}")
    sys.exit(0)
tid, period, duration, cpu, group, csv, metadata, ready, policy = sys.argv[1:]
pathlib.Path(csv).write_text(
    "read_begin_ns,read_end_ns,event_count,enabled_ns,running_ns,cycles,instructions,event2,event3,read_error\n"
    "1000000,1000010,4,100,100,200,300,100,10,0\n"
    "1100000,1100010,4,200,200,400,600,200,20,0\n"
    "1200000,1200010,4,300,300,600,900,300,30,0\n")
pathlib.Path(metadata).write_text(json.dumps({"sample_count": 3,
    "target_tid": int(tid), "clock": "CLOCK_MONOTONIC", "period_ns_requested": int(period)*1000,
    "scope": "target_thread_user_only_no_inherit", "stopped_signal": 15,
    "event_names": ["cycles", "instructions", "generic_cache_references", "generic_cache_misses"]}))
if group in ("auto", "all"):
    topics = {"base": [], "cache": ["generic_cache_references", "generic_cache_misses"],
        "branch": ["branch_instructions", "branch_misses"], "stall": ["stalled_frontend", "stalled_backend"],
        "l1d": ["l1d_read_access", "l1d_read_misses"], "llc": ["llc_read_access", "llc_read_misses"],
        "dtlb": ["dtlb_read_access", "dtlb_read_misses"]}
    groups = [{"id": identity, "event_names": ["cycles", "instructions", *events],
               "requested_event_names": ["cycles", "instructions", *events], "sample_count": 3,
               "status": "active", "enable_error": 0, "unavailable_events": []} for identity, events in topics.items()]
    with pathlib.Path(csv).open("w") as output:
        writer = csv_module.writer(output)
        writer.writerow(["sample_index", "group_id", "read_begin_ns", "read_end_ns", "event_count", "enabled_ns", "running_ns",
                         "event0", "event1", "event2", "event3", "read_error", "reader_cpu"])
        for tick in range(3):
            for definition in groups:
                size = len(definition["event_names"]); n = tick + 1
                writer.writerow([tick, definition["id"], 1000000 + tick*100000, 1000010 + tick*100000, size, n*100, n*100,
                                 n*200, n*300, n*100 if size == 4 else "", n*10 if size == 4 else "", 0, 0])
    pathlib.Path(metadata).write_text(json.dumps({"schema_version": "thread_pmu_capture/v2", "format": "grouped_long_csv",
        "sampler_version": "1.2.0", "group": group, "groups": groups, "sample_count": 21, "tick_count": 3,
        "target_tid": int(tid), "clock": "CLOCK_MONOTONIC", "period_ns_requested": int(period)*1000,
        "period_ns_effective": int(period)*1000, "scope": "target_thread_user_only_no_inherit", "stopped_signal": 15}))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(5))
pathlib.Path(ready).write_text("ready\n")
time.sleep(5)
''')
        self.sampler.chmod(0o755)

    def run_capture(self, bundle: str, pid_reply: str, *, launch: bool = False, group: str | None = None) -> tuple[dict, ScriptDevice]:
        operation = {"title": "启动应用", "operation": "launch"} if launch else {
            "title": "操作窗口", "operation": "wait", "seconds": .02}
        plan = self.manager.create_plan({"template_id": "custom", "steps": [operation],
            "profile": {"bundle": bundle, "ability": "MainAbility", "guard_seconds": 0,
                        "hold_seconds": 0, "prepare_settle_seconds": 0},
            "capture": {"selected_step_ids": ["step_01"], "collectors": ["pmu"]}})
        # execute_plan receives an already prepared sampler path. The backend
        # installation contract has separate tests; no device prep is invoked.
        plan["capture"]["pmu"]["binary_path"] = str(self.sampler)
        if group is not None:
            plan["capture"]["pmu"]["group"] = group
        root = self.root / bundle
        device = ScriptDevice(root, bundle, pid_reply)
        if launch:
            # The action command is the only device-only executable in this
            # scenario; replace it with a stub in the local shell environment.
            bins = self.root / "bin"
            bins.mkdir(exist_ok=True)
            aa = bins / "aa"
            aa.write_text("#!/bin/sh\nexit 0\n")
            aa.chmod(0o755)
            from unittest.mock import patch
            with patch.dict(os.environ, {"PATH": str(bins) + os.pathsep + os.environ["PATH"]}):
                manifest = execute_plan(plan, device, root / "run", "cross_app_launch")
        else:
            manifest = execute_plan(plan, device, root / "run", "cross_app_capture")
        self.assertNotEqual(manifest["status"], "failed", manifest["quality"])
        self.assertEqual(manifest["steps"][0]["status"], "succeeded", manifest["quality"])
        self.assertIn("pidof " + bundle + " || true", device.commands)
        self.assertFalse(any("com.huawei.hmos.photos" in command for command in device.commands))
        return manifest, device

    def test_different_apps_reuse_one_sampler_with_their_own_main_thread(self) -> None:
        for bundle, tid in (("com.example.reader", 3123), ("com.example.player", 9456)):
            with self.subTest(bundle=bundle):
                manifest, device = self.run_capture(bundle, str(tid), group="cache")
                step = manifest["steps"][0]
                self.assertEqual(step["target_pid"], tid)
                self.assertEqual(step["collector_status"]["pmu"], "collected")
                self.assertEqual(step["pmu_validation"]["scope"], "target_main_thread")
                self.assertEqual(manifest["pmu_validation_schema"], "pmu_capture_validation/v2")
                self.assertTrue(step["pmu_validation"]["valid"], step["pmu_validation"])
                self.assertIn("pmu_bin=" + str(self.sampler), device.scripts[0])
                metadata = json.loads(next(path for path in (self.root / bundle / "run").rglob("pmu.json")).read_text())
                self.assertEqual(metadata["target_tid"], tid)
        invocations = [json.loads(line) for line in self.calls.read_text().splitlines()]
        samples = [arguments for arguments in invocations if arguments[0].isdigit()]
        self.assertEqual([arguments[0] for arguments in samples], ["3123", "9456"])
        self.assertTrue(all(arguments[1:5] == ["100", "10000", "0", "cache"] for arguments in samples))

    def test_default_auto_reuses_sampler_and_preserves_each_apps_grouped_target(self) -> None:
        for bundle, tid in (("com.example.notes", 4001), ("com.example.mail", 8237)):
            with self.subTest(bundle=bundle):
                manifest, _ = self.run_capture(bundle, str(tid))
                step = manifest["steps"][0]
                self.assertEqual(step["target_pid"], tid)
                self.assertEqual(step["collector_status"]["pmu"], "collected")
                self.assertTrue(step["pmu_validation"]["valid"])
                self.assertEqual(len(step["pmu_validation"]["available_event_names"]), 14)
                self.assertEqual(manifest["runner_version"], "1.2.15")
                self.assertEqual(manifest["pmu_validation_schema"], "pmu_capture_validation/v3")
                self.assertEqual(step["pmu_validation"]["schema_version"], manifest["pmu_validation_schema"])
                self.assertEqual(step["pmu_validation"]["metadata"]["target_tid"], tid)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        samples = [args for args in calls if args[0].isdigit()]
        self.assertEqual([args[0] for args in samples], ["4001", "8237"])
        self.assertTrue(all(args[4] == "auto" for args in samples))

    def assert_unresolved(self, manifest: dict, device: ScriptDevice) -> None:
        step = manifest["steps"][0]
        self.assertIsNone(step["target_pid"])
        self.assertEqual(step["collector_status"]["pmu"], "unavailable")
        self.assertEqual(manifest["status"], "partial")
        self.assertIn("PMU_TARGET_NOT_RESOLVED", [item["code"] for item in step["pmu_validation"]["diagnostics"]])
        self.assertIn("No unique pre-action target PID; PMU not started", device.scripts[0])
        self.assertFalse(self.calls.exists(), "Unresolved targets must not query or start the sampler")

    def test_multiple_processes_are_not_silently_reduced_to_one_target(self) -> None:
        self.assert_unresolved(*self.run_capture("com.example.browser", "3123 9456\n"))

    def test_absent_process_preserves_pmu_unavailable_reason(self) -> None:
        self.assert_unresolved(*self.run_capture("com.example.reader", ""))

    def test_cold_launch_without_preaction_pid_does_not_claim_pmu_capture(self) -> None:
        manifest, device = self.run_capture("com.example.player", "", launch=True)
        self.assert_unresolved(manifest, device)
        self.assertIn("aa start -a MainAbility -b com.example.player", device.scripts[0])
        self.assertEqual(manifest["steps"][0]["launch_process_state"], "unknown")


if __name__ == "__main__":
    unittest.main()
