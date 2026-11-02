"""Sampler numeric parser and device-shell compatibility tests without a phone."""
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.scenario_capture.runner import pmu_capability_guard, validate_pmu


class PmuSamplerCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self): self.tmp.cleanup()

    def guard(self, period, capability=None, *, pretty=False, group=None, without_tr=False):
        out = self.root / "result"
        out.mkdir(exist_ok=True)
        binary = self.root / "stub-sampler"
        if capability is None:
            script = '#!/bin/sh\nprintf "legacy usage\\n" >&2\nexit 2\n'
        else:
            body = json.dumps(capability, indent=2 if pretty else None)
            script = '#!/bin/sh\nprintf "%s\\n" ' + shlex.quote(body) + '\n'
        binary.write_text(script); binary.chmod(0o700)
        shell = '\n'.join(['set -u', 'out=' + shlex.quote(str(out)), 'pmu_bin=' + shlex.quote(str(binary)), *pmu_capability_guard(period, group=group),
                           'printf "%s" "$pmu_compatible" > "$out/compatible.txt"'])
        env = None
        if without_tr:
            # Real HarmonyOS device /bin has these tools but no tr. Restrict
            # PATH instead of relying on the host's more complete utilities.
            utilities = self.root / "device-bin"
            utilities.mkdir(exist_ok=True)
            for name in ("sed", "grep", "head", "cat", "sleep"):
                target = utilities / name
                if not target.exists():
                    target.symlink_to(shutil.which(name))
            env = {**os.environ, "PATH": str(utilities)}
        result = subprocess.run(['/bin/sh', '-c', shell], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return out, json.loads((out / 'pmu_compatibility.json').read_text())

    def capabilities(self, minimum=10):
        return {"schema_version": "gallery_thread_pmu_capabilities/v1", "sampler_version": "1.1.0", "period_us_min": minimum, "period_us_max": 10000}

    def test_reported_ten_us_supported_with_compact_or_pretty_json(self):
        for pretty in (False, True):
            out, value = self.guard(10, self.capabilities(), pretty=pretty)
            self.assertEqual(value["status"], "supported")
            self.assertEqual((out / 'compatible.txt').read_text(), '1')

    def test_auto_mode_requires_advertised_group_and_supports_pretty_capabilities(self):
        for pretty in (False, True):
            for group in ("auto", "all"):
                caps = {**self.capabilities(), "sampler_version": "1.2.0", "event_groups": ["auto", "all", "cache"]}
                out, value = self.guard(100, caps, pretty=pretty, group=group)
                self.assertEqual(value["status"], "supported")
                self.assertEqual((out / "compatible.txt").read_text(), "1")
        for caps in (None, self.capabilities(), {**self.capabilities(), "event_groups": ["cache"]}):
            out, value = self.guard(100, caps, group="auto")
            self.assertEqual(value["status"], "multi_mode_upgrade_required")
            self.assertEqual((out / "compatible.txt").read_text(), "0")
            diagnostic = validate_pmu(out, collector_state="unavailable")
            self.assertEqual(diagnostic["diagnostics"][0]["code"], "PMU_MULTI_GROUP_UPGRADE_REQUIRED")

    def test_device_without_tr_supports_all_modes_and_pretty_capabilities(self):
        for pretty in (False, True):
            for group in ("auto", "all", "cache"):
                caps = {**self.capabilities(), "sampler_version": "1.2.0", "event_groups": ["auto", "all", "cache"]}
                out, value = self.guard(10, caps, pretty=pretty, group=group, without_tr=True)
                self.assertEqual(value["status"], "supported")
                self.assertEqual((out / "compatible.txt").read_text(), "1")

    def test_legacy_binary_ten_us_requires_upgrade_while_existing_requests_remain(self):
        out, value = self.guard(10)
        self.assertEqual(value["status"], "upgrade_required")
        self.assertEqual((out / 'compatible.txt').read_text(), '0')
        diagnostic = validate_pmu(out, collector_state='unavailable')
        self.assertEqual(diagnostic['diagnostics'][0]['code'], 'PMU_SAMPLER_UPGRADE_REQUIRED')
        _, value = self.guard(100)
        self.assertEqual(value['status'], 'legacy_unverified')

    def test_reported_minimum_is_enforced_and_explained(self):
        out, value = self.guard(10, self.capabilities(20))
        self.assertEqual(value['status'], 'period_unsupported')
        self.assertEqual(value['reported_min_period_us'], 20)
        diagnostic = validate_pmu(out, collector_state='unavailable')
        self.assertEqual(diagnostic['diagnostics'][0]['code'], 'PMU_PERIOD_UNSUPPORTED')

    def test_malformed_capabilities_cannot_advertise_ten_us(self):
        for value in ('10;echo BAD', -1, '00010', 184467440737095516160):
            capability = self.capabilities(); capability['period_us_min'] = value
            _, result = self.guard(10, capability)
            self.assertEqual(result['status'], 'upgrade_required')
        capability = self.capabilities(); capability['schema_version'] = 'unrecognized/v1'
        _, result = self.guard(10, capability)
        self.assertEqual(result['status'], 'upgrade_required')


class PmuSamplerNumericParserTests(unittest.TestCase):
    """Compile the exact portable parse_decimal function from production C.

    The device-specific remainder is separately cross-compiled with its SDK;
    this harness never opens perf events or runs a sampler.
    """
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if not compiler: raise unittest.SkipTest('A host C compiler is required for numeric parser tests')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        source = (Path(__file__).resolve().parents[2] / 'tools/scenario_capture/thread_pmu_sampler.c').read_text()
        function = source[source.index('static int parse_decimal('):source.index('\ntypedef struct')]
        harness = '#include <errno.h>\n#include <stdint.h>\n#include <stdlib.h>\n#include <stdio.h>\n' + function + '\nint main(int argc, char **argv) { if (argc != 4) return 3; uint64_t value; int ok = parse_decimal(argv[1], strtoull(argv[2], NULL, 10), strtoull(argv[3], NULL, 10), &value); if (ok) printf("%llu", (unsigned long long)value); return ok ? 0 : 2; }\n'
        path = cls.root / 'parser.c'; path.write_text(harness)
        cls.binary = cls.root / 'parser'
        result = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror', str(path), '-o', str(cls.binary)], capture_output=True, text=True)
        if result.returncode: raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'tmp'): cls.tmp.cleanup()

    def parsed(self, value, minimum=10, maximum=10000):
        return subprocess.run([str(self.binary), value, str(minimum), str(maximum)], capture_output=True, text=True)

    def test_period_boundaries_and_overflow(self):
        for value in ('10', '20', '100', '10000'):
            self.assertEqual(self.parsed(value).returncode, 0)
        for value in ('9', '10001', '0', '-1', '+10', ' 10', '10us', '', '1e1', '184467440737095516160'):
            self.assertEqual(self.parsed(value).returncode, 2, value)

    def test_pid_reader_and_duration_ranges(self):
        self.assertEqual(self.parsed('2147483647', 1, 2147483647).returncode, 0)
        self.assertEqual(self.parsed('2147483648', 1, 2147483647).returncode, 2)
        self.assertEqual(self.parsed('0', 0, 1023).returncode, 0)
        self.assertEqual(self.parsed('1024', 0, 1023).returncode, 2)
        self.assertEqual(self.parsed('10001', 1, 10000).returncode, 2)


if __name__ == '__main__': unittest.main()
