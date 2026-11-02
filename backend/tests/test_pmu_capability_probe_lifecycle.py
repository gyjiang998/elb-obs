"""Execute device-shell probe lifecycle against disposable local fake CLIs."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.scenario_capture.runner import build_step_script, pmu_capability_guard, validate_pmu


class PmuCapabilityProbeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / "out"; self.out.mkdir()
        self.bin = self.root / "bin"; self.bin.mkdir()
        self.sampler = self.bin / "fake-sampler"
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}

    def executable(self, name, body, *, python=False):
        path = self.bin / name
        path.write_text(("#!" + sys.executable + "\n" if python else "#!/bin/sh\n") + body)
        path.chmod(0o700)
        return path

    def source(self, period=10):
        return "\n".join(["set -u", "out=" + shlex.quote(str(self.out)), "pmu_bin=" + shlex.quote(str(self.sampler)),
            *pmu_capability_guard(period, timeout_seconds=.5)])

    def run_guard(self, period=10):
        result = subprocess.run(["sh", "-c", self.source(period)], env=self.env, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.out / "pmu_compatibility.json").read_text()), json.loads((self.out / "pmu_capability_probe.json").read_text())

    def assert_process_stopped(self, pid):
        # A just-orphaned zombie has stopped executing and awaits OS reaping.
        result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith("Z"), result.stdout)

    def test_empty_failed_and_prefixed_outputs_never_claim_ten_us(self):
        cases = ["exit 0\n", "exit 2\n", "printf '%s\\n' 'log prefix' '{\"schema_version\":\"gallery_thread_pmu_capabilities/v1\",\"period_us_min\":10,\"period_us_max\":10000}'\n"]
        for body in cases:
            with self.subTest(body=body):
                self.executable("fake-sampler", body)
                compatibility, probe = self.run_guard()
                self.assertEqual(compatibility["status"], "upgrade_required")
                self.assertEqual(probe["status"], "exited")
                self.assertFalse(probe["timed_out"])
        self.executable("fake-sampler", "exit 2\n")
        compatibility, probe = self.run_guard(100)
        self.assertEqual(compatibility["status"], "legacy_unverified")
        self.assertEqual(probe["exit_code"], 2)

    def test_timeout_kills_term_ignoring_probe_and_preserves_legacy_period(self):
        pid_file = self.root / "probe.pid"
        self.executable("fake-sampler", f"trap '' TERM\necho $$ > {shlex.quote(str(pid_file))}\nexec sleep 30\n")
        for period in (10, 100):
            started = time.monotonic()
            compatibility, probe = self.run_guard(period)
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(probe["timed_out"])
            self.assertEqual(probe["status"], "timeout")
            self.assertNotEqual(probe["exit_code"], 0)
            self.assertEqual(compatibility["capabilities_exit_code"], probe["exit_code"])
            self.assertEqual(compatibility["status"], "upgrade_required" if period == 10 else "legacy_unverified")
            self.assert_process_stopped(int(pid_file.read_text()))

    def test_process_group_cleanup_also_stops_probe_children(self):
        # Use a native shim: two interpreter startups are unrelated to timeout
        # correctness and become flaky when the larger suite loads the host.
        compiler = shutil.which("cc")
        if not compiler: self.skipTest("A host C compiler is required for a setsid shim")
        source = self.root / "setsid.c"
        source.write_text('#include <unistd.h>\nint main(int argc,char**argv){if(argc<2||setsid()<0)return 125;execvp(argv[1],argv+1);return 126;}\n')
        subprocess.run([compiler, str(source), "-o", str(self.bin / "setsid")], check=True, capture_output=True)
        pids = self.root / "children.json"
        self.executable("fake-sampler", "trap '' TERM\nsleep 30 & child=$!\n"
                        f"printf '[%s,%s]' $$ $child > {shlex.quote(str(pids))}\nwait $child\n")
        _, probe = self.run_guard()
        self.assertEqual(probe["process_group"], 1)
        for pid in json.loads(pids.read_text()):
            self.assert_process_stopped(pid)

    def test_cancel_during_probe_cleans_pid_and_records_real_exit(self):
        pid_file = self.root / "probe.pid"
        self.executable("fake-sampler", f"echo $$ > {shlex.quote(str(pid_file))}\nexec sleep 30\n")
        guard = self.source()
        guard = guard.replace("pmu_probe_ticks=10;", "pmu_probe_ticks=100;")
        guard = "trap 'pmu_probe_status=cancelled; stop_pmu_capability_probe; exit 130' TERM\n" + guard
        process = subprocess.Popen(["sh", "-c", guard], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.monotonic() + 1
            while not pid_file.exists() and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(pid_file.exists())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=2), 130)
            receipt = json.loads((self.out / "pmu_capability_probe.json").read_text())
            self.assertEqual(receipt["status"], "cancelled")
            self.assertNotEqual(receipt["exit_code"], 0)
            self.assert_process_stopped(int(pid_file.read_text()))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL); process.wait()

    def test_excessive_probe_output_is_bounded_and_not_supported(self):
        self.executable("fake-sampler", "import sys\nsys.stdout.write('x'*131072)\n", python=True)
        compatibility, receipt = self.run_guard()
        self.assertEqual(compatibility["status"], "upgrade_required")
        self.assertNotEqual(receipt["exit_code"], 0)
        self.assertLessEqual((self.out / "pmu_capabilities.json").stat().st_size, 16384)

    def test_real_step_watchdog_can_interrupt_probe_and_complete_cleanup(self):
        self.executable("fake-sampler", "exec sleep 30\n")
        profile = {"step_timeout_seconds": .2, "hold_seconds": 0, "prepare_settle_seconds": 0, "guard_seconds": 0}
        capture = {"selected_step_ids": ["step_01"], "collectors": ["pmu"],
                   "pmu": {"binary_path": str(self.sampler), "period_us": 10, "duration_ms": 100, "reader_cpu": 0, "group": "cache"}}
        step = {"step_id": "step_01", "ordinal": 1, "slug": "test", "operation": "wait"}
        source = build_step_script(step, [{"name": "TEST", "command": ["true"]}], profile, capture, str(self.out), "", "123")
        result = subprocess.run(["sh", "-c", source], env=self.env, capture_output=True, text=True, timeout=2)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual((self.out / "status.txt").read_text().strip(), "130")
        self.assertFalse((self.out / "owner.pid").exists())
        self.assertEqual(json.loads((self.out / "pmu_capability_probe.json").read_text())["status"], "cancelled")

    def test_unavailable_probe_finishes_before_trace_and_keeps_other_outputs(self):
        calls = self.root / "calls"
        trace_root = self.root / "trace"; trace_root.mkdir()
        (trace_root / "tracing_on").write_text("0")
        (trace_root / "trace_marker").write_text("")
        (trace_root / "trace_clock").write_text("[mono]")
        self.executable("fake-sampler", f"echo \"probe $1\" >> {shlex.quote(str(calls))}\nexec sleep 30\n")
        self.executable("hitrace", "import sys\nfrom pathlib import Path\n"
                        f"root=Path({str(trace_root)!r}); calls=Path({str(calls)!r})\n"
                        "with calls.open('a') as f: f.write('hitrace '+sys.argv[1]+'\\n')\n"
                        "if '--trace_begin' in sys.argv: (root/'tracing_on').write_text('1')\n"
                        "if '--trace_finish' in sys.argv:\n (root/'tracing_on').write_text('0')\n Path(sys.argv[sys.argv.index('-o')+1]).write_text('fake trace retained')\n", python=True)
        self.executable("hilog", "printf 'fake hilog retained\\n'\nexec sleep 30\n")
        profile = {"step_timeout_seconds": 5, "hold_seconds": 0, "prepare_settle_seconds": 0,
                   "guard_seconds": .2, "trace_buffer_kb": 1024, "trace_categories": ["sched"]}
        capture = {"selected_step_ids": ["step_01"], "collectors": ["trace", "hilog", "pmu"],
                   "pmu": {"binary_path": str(self.sampler), "period_us": 10, "duration_ms": 100, "reader_cpu": 0, "group": "cache"}}
        step = {"step_id": "step_01", "ordinal": 1, "slug": "test", "operation": "wait"}
        with patch("tools.scenario_capture.runner.pmu_capability_guard", side_effect=lambda p, **kwargs: pmu_capability_guard(p, timeout_seconds=.5, **kwargs)):
            source = build_step_script(step, [{"name": "TEST", "command": ["true"]}], profile, capture, str(self.out), str(trace_root), "123")
        result = subprocess.run(["sh", "-c", source], env=self.env, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.out / "status.txt").read_text().strip(), "0")
        self.assertEqual((self.out / "pmu_status.txt").read_text().strip(), "unavailable")
        self.assertIn("fake trace retained", (self.out / "trace.ftrace").read_text())
        self.assertIn("fake hilog retained", (self.out / "hilog.log").read_text())
        self.assertEqual(calls.read_text().splitlines(), ["probe --capabilities", "hitrace --trace_begin", "hitrace --trace_finish"])
        self.assertEqual(validate_pmu(self.out, collector_state="unavailable")["diagnostics"][0]["code"], "PMU_SAMPLER_UPGRADE_REQUIRED")

    def test_term_ignoring_sampler_and_hilog_are_killed_with_bounded_cleanup(self):
        sampler_body = (
            "import json,signal,sys,time\n"
            "from pathlib import Path\n"
            "if '--capabilities' in sys.argv:\n"
            " print(json.dumps({'schema_version':'gallery_thread_pmu_capabilities/v1','period_us_min':10,'period_us_max':10000,'event_groups':['cache']})); raise SystemExit(0)\n"
            "if '--clock' in sys.argv: print('{}'); raise SystemExit(0)\n"
            "Path(sys.argv[8]).write_text('ready')\n"
            "Path(sys.argv[6]).write_text('sample_count\\n1\\n')\n"
            "Path(sys.argv[7]).write_text(json.dumps({'schema_version':'thread_pmu_capture/v2','sample_count':1}))\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True: time.sleep(.05)\n"
        )
        self.executable("fake-sampler", sampler_body, python=True)
        self.executable("hilog", "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('event', flush=True)\nwhile True: time.sleep(.05)\n", python=True)
        profile = {"step_timeout_seconds": 2, "hold_seconds": 0, "prepare_settle_seconds": 0,
                   "guard_seconds": 0}
        capture = {"selected_step_ids": ["step_01"], "collectors": ["hilog", "pmu"],
                   "pmu": {"binary_path": str(self.sampler), "period_us": 10, "duration_ms": 100,
                           "reader_cpu": 0, "group": "cache"}}
        step = {"step_id": "step_01", "ordinal": 1, "slug": "test", "operation": "wait"}
        source = build_step_script(step, [{"name": "TEST", "command": ["true"]}], profile, capture,
                                   str(self.out), "", "123")
        started = time.monotonic()
        result = subprocess.run(["sh", "-c", source], env=self.env, capture_output=True, text=True, timeout=7)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = (self.out / "cleanup_events.log").read_text()
        self.assertIn("pmu_sampler KILL", events)
        self.assertIn("hilog KILL", events)
        self.assertEqual((self.out / "cleanup_phase.txt").read_text().strip(), "cleanup_complete")
        self.assertFalse((self.out / "owner.pid").exists())

    def test_term_ignoring_hitrace_finisher_is_bounded_and_marks_step_failed(self):
        trace_root = self.root / "trace-hang"; trace_root.mkdir()
        (trace_root / "tracing_on").write_text("0")
        (trace_root / "trace_marker").write_text("")
        (trace_root / "trace_clock").write_text("[mono]")
        self.executable("hitrace", "import signal,sys,time\nfrom pathlib import Path\n"
                        f"root=Path({str(trace_root)!r})\n"
                        "if '--trace_begin' in sys.argv: root.joinpath('tracing_on').write_text('1'); raise SystemExit(0)\n"
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                        "while True: time.sleep(.05)\n", python=True)
        profile = {"step_timeout_seconds": 2, "hold_seconds": 0, "prepare_settle_seconds": 0,
                   "guard_seconds": 0, "trace_buffer_kb": 1024, "trace_categories": ["sched"]}
        capture = {"selected_step_ids": ["step_01"], "collectors": ["trace"],
                   "pmu": {"binary_path": str(self.sampler), "period_us": 10, "duration_ms": 100,
                           "reader_cpu": 0, "group": "cache"}}
        step = {"step_id": "step_01", "ordinal": 1, "slug": "test", "operation": "wait"}
        source = build_step_script(step, [{"name": "TEST", "command": ["true"]}], profile, capture,
                                   str(self.out), str(trace_root), "123")
        started = time.monotonic()
        result = subprocess.run(["sh", "-c", source], env=self.env, capture_output=True, text=True, timeout=7)
        self.assertLess(time.monotonic() - started, 6)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trace_finisher KILL", (self.out / "cleanup_events.log").read_text())
        self.assertEqual((self.out / "cleanup_phase.txt").read_text().strip(), "cleanup_complete")
        self.assertEqual((self.out / "status.txt").read_text().strip(), "1")
        self.assertFalse((self.out / "owner.pid").exists())


if __name__ == "__main__": unittest.main()
