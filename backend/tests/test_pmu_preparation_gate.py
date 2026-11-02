"""A failed sampler preparation never retries its executable during capture."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.scenario_capture.runner import build_step_script, validate_pmu


class PmuPreparationGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.out = self.root / "out"
        self.calls = self.root / "calls"
        self.trace = self.root / "tracing"
        self.trace.mkdir()
        (self.trace / "tracing_on").write_text("0")
        (self.trace / "trace_marker").write_text("")
        (self.trace / "trace_clock").write_text("[mono]")
        self.sampler = self.executable("sampler", f"echo sampler_called >> {shlex.quote(str(self.calls))}\nexit 99\n")
        self.executable("hitrace", "import sys\nfrom pathlib import Path\n"
                        f"root=Path({str(self.trace)!r}); calls=Path({str(self.calls)!r})\n"
                        "with calls.open('a') as stream: stream.write('hitrace '+sys.argv[1]+'\\n')\n"
                        "if '--trace_begin' in sys.argv: (root/'tracing_on').write_text('1')\n"
                        "if '--trace_finish' in sys.argv:\n (root/'tracing_on').write_text('0')\n Path(sys.argv[sys.argv.index('-o')+1]).write_text('trace retained')\n", python=True)
        self.executable("hilog", "printf 'hilog retained\\n'\nexec sleep 30\n")
        self.profile = {"step_timeout_seconds": 5, "hold_seconds": 0, "prepare_settle_seconds": 0,
                        "guard_seconds": .1, "trace_buffer_kb": 1024, "trace_categories": ["sched"]}
        self.step = {"step_id": "step_01", "ordinal": 1, "slug": "test", "operation": "wait"}

    def tearDown(self):
        self.temporary.cleanup()

    def executable(self, name, body, *, python=False):
        path = self.bin / name
        path.write_text(("#!" + sys.executable + "\n" if python else "#!/bin/sh\n") + body)
        path.chmod(0o700)
        return path

    def run_gate(self, gate, *, pid="123"):
        capture = {"selected_step_ids": ["step_01"], "collectors": ["trace", "hilog", "pmu"],
                   "pmu": {"binary_path": str(self.sampler), "period_us": 100, "duration_ms": 100,
                           "reader_cpu": 0, "group": "cache", "preparation": gate}}
        script = build_step_script(self.step, [{"name": "ACTION", "command": ["true"]}], self.profile,
                                   capture, str(self.out), str(self.trace), pid)
        result = subprocess.run(["sh", "-c", script], env={**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]},
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        return script

    def test_rejected_sampler_is_never_probed_clocked_or_started_but_trace_hilog_continue(self):
        gate = {"schema_version": "pmu_sampler_execution_gate/v1", "allow_execution": False,
                "status": "incompatible", "code": "PMU_CAPABILITY_TIMEOUT", "message": "采样器能力查询超时，本轮不再执行。"}
        script = self.run_gate(gate)
        self.assertEqual(self.calls.read_text().splitlines(), ["hitrace --trace_begin", "hitrace --trace_finish"])
        self.assertNotIn(str(self.sampler), script)
        self.assertEqual((self.out / "trace.ftrace").read_text(), "trace retained")
        self.assertIn("hilog retained", (self.out / "hilog.log").read_text())
        self.assertEqual((self.out / "hilog_status.txt").read_text().strip(), "stopped")
        self.assertEqual((self.out / "status.txt").read_text().strip(), "0")
        self.assertEqual((self.out / "pmu_status.txt").read_text().strip(), "unavailable")
        self.assertEqual(json.loads((self.out / "pmu_preparation.json").read_text()), gate)
        self.assertFalse((self.out / "pmu_capability_probe.json").exists())
        self.assertFalse((self.out / "clock_before.json").exists())
        self.assertFalse((self.out / "clock_after.json").exists())
        result = validate_pmu(self.out, collector_state="unavailable")
        self.assertFalse(result["valid"])
        self.assertEqual(result["diagnostics"][0]["code"], gate["code"])
        self.assertEqual(result["diagnostics"][0]["message"], gate["message"])
        self.assertEqual(result["sampler_preparation"], gate)

    def test_missing_target_does_not_replace_preparation_reason_and_messages_are_shell_quoted(self):
        marker = self.root / "must-not-exist"
        gate = {"allow_execution": False, "status": "unavailable", "code": "PMU_BUNDLE_SOURCE_MISMATCH",
                "message": f"无法使用 'old' bundle; $(touch {marker}) `touch {marker}`"}
        self.run_gate(gate, pid="")
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads((self.out / "pmu_preparation.json").read_text()), gate)
        result = validate_pmu(self.out, collector_state="unavailable")
        self.assertEqual(result["diagnostics"][0]["code"], "PMU_BUNDLE_SOURCE_MISMATCH")
        self.assertEqual(result["reason"], gate["message"])

    def test_gate_takes_priority_over_stale_counter_or_compatibility_files(self):
        self.out.mkdir()
        gate = {"allow_execution": False, "status": "failed", "code": "PMU_DEPLOY_FAILED", "message": "下发失败"}
        (self.out / "pmu_preparation.json").write_text(json.dumps(gate))
        (self.out / "pmu_compatibility.json").write_text('{"status":"upgrade_required"}')
        (self.out / "pmu.json").write_text('{"open_error":13}')
        result = validate_pmu(self.out)
        self.assertEqual(result["diagnostics"][0]["code"], "PMU_DEPLOY_FAILED")
        self.assertFalse(result["quality_complete"])


if __name__ == "__main__":
    unittest.main()
