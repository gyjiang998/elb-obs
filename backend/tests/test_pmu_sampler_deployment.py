"""Deployment prepares the repository PMU bundle without a compiler or device."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.tests.test_pmu_sampler_bundle import target_elf
from backend.tools import deployment_preflight
from tools.scenario_capture.pmu_bundle import TARGET, verify_bundle


ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "deploy/linux/heteroctl"


class PmuPreflightTests(unittest.TestCase):
    def test_missing_pmu_is_separate_optional_capability(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "HETERO_DEPLOYMENT_MODE": "device", "HETERO_PMU_SAMPLER_BUNDLE_ROOT": directory,
        }):
            rows = deployment_preflight.collect_resource_checks()
        row = next(item for item in rows if item["check_id"] == "pmu.bundle")
        self.assertEqual(row["capability"], "pmu")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("heteroctl pmu", row["detail"])
        self.assertNotIn("复制与当前源码", row["detail"])

    def test_pmu_failure_only_blocks_explicit_requirement(self):
        rows = [{"check_id": "pmu.bundle", "status": "unavailable", "capability": "pmu", "path": "missing", "detail": "prepare sampler"}]
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "device"}), \
             patch.object(deployment_preflight, "collect_resource_checks", return_value=rows), \
             patch("builtins.print"):
            for flags, expected in (([], 0), (["--require-device"], 0), (["--require-pmu"], 1)):
                with self.subTest(flags=flags), patch.object(sys, "argv", ["preflight", "--resources-only", *flags]):
                    self.assertEqual(deployment_preflight.main(), expected)

    def test_analysis_deployment_does_not_open_pmu_assets(self):
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "analysis"}), \
             patch.object(deployment_preflight, "verify_bundle") as verify:
            self.assertEqual(deployment_preflight.collect_resource_checks(), [])
        verify.assert_not_called()


@unittest.skipIf(getattr(os, "geteuid", lambda: 0)() == 0, "heteroctl rejects root")
class PmuControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".pmu-deploy-test-", dir=ROOT)
        self.root = Path(self.temporary.name).resolve()
        self.runtime = self.root / "runtime"
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("HETERO_")}
        self.environment["HETERO_PYTHON_BIN"] = sys.executable

    def tearDown(self):
        self.temporary.cleanup()

    def run_controller(self, command, *, extra_environment=None):
        return subprocess.run([str(CONTROLLER), command, "--runtime-root", str(self.runtime)],
                              cwd=ROOT, env={**self.environment, **(extra_environment or {})},
                              capture_output=True, text=True, timeout=30)

    def make_sdk(self):
        native = self.root / "sdk" / "native"
        compiler = native / "llvm/bin/clang"
        compiler.parent.mkdir(parents=True)
        payload = base64.b64encode(target_elf()).decode()
        compiler.write_text(
            f"#!{sys.executable}\nimport base64,pathlib,sys\n"
            "if sys.argv[1:] == ['--version']:\n print('OHOS test compiler'); sys.exit(0)\n"
            f"assert '--target={TARGET}' in sys.argv\n"
            "assert '-std=c11' in sys.argv and '-Werror' in sys.argv\n"
            f"pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_bytes(base64.b64decode({payload!r}))\n"
        )
        compiler.chmod(0o755)
        for item in ("usr/include/linux/perf_event.h", f"usr/lib/{TARGET}/Scrt1.o", f"usr/lib/{TARGET}/libc.so"):
            path = native / "sysroot" / item
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        return native

    def test_pmu_environment_defaults_and_nonexisting_output_overrides_persist(self):
        first = self.run_controller("init")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        env_file = self.runtime / "platform.env"
        self.assertIn('HETERO_OPENHARMONY_NATIVE_SDK_ROOT=""', env_file.read_text())
        self.assertIn(str(self.runtime / "assets/pmu-sampler"), env_file.read_text())
        sdk = self.make_sdk()
        output = self.root / "custom" / "not-created-bundle"
        second = self.run_controller("init", extra_environment={
            "HETERO_OPENHARMONY_NATIVE_SDK_ROOT": str(sdk), "HETERO_PMU_SAMPLER_BUNDLE_ROOT": str(output),
        })
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        contents = env_file.read_text()
        self.assertIn(f'HETERO_OPENHARMONY_NATIVE_SDK_ROOT="{sdk}"', contents)
        self.assertIn(f'HETERO_PMU_SAMPLER_BUNDLE_ROOT="{output}"', contents)
        self.assertFalse(output.exists())

    def test_explicit_pmu_build_uses_sdk_and_persisted_custom_asset_root(self):
        sdk = self.make_sdk()
        output = self.root / "custom-bundle"
        result = self.run_controller("pmu", extra_environment={
            "HETERO_OPENHARMONY_NATIVE_SDK_ROOT": str(sdk), "HETERO_PMU_SAMPLER_BUNDLE_ROOT": str(output),
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(verify_bundle(output)["target"], TARGET)
        env_file = self.runtime / "platform.env"
        contents = env_file.read_text().replace(f'HETERO_OPENHARMONY_NATIVE_SDK_ROOT="{sdk}"', 'HETERO_OPENHARMONY_NATIVE_SDK_ROOT=""')
        env_file.write_text(contents)
        reused = self.run_controller("pmu")
        self.assertEqual(reused.returncode, 0, reused.stdout + reused.stderr)
        self.assertIn("reused", reused.stdout)
        self.assertNotIn("交叉编译", reused.stdout)
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "device", "HETERO_PMU_SAMPLER_BUNDLE_ROOT": str(output)}):
            row = next(item for item in deployment_preflight.collect_resource_checks() if item["check_id"] == "pmu.bundle")
        self.assertEqual(row["status"], "ready")

    def test_explicit_pmu_without_sdk_prepares_repository_bundle(self):
        result = self.run_controller("pmu")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.runtime / "assets/pmu-sampler"
        self.assertEqual(verify_bundle(output)["target"], TARGET)
        before = (output / "manifest.json").stat().st_mtime_ns
        repeated = self.run_controller("pmu")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual((output / "manifest.json").stat().st_mtime_ns, before)
        self.assertNotIn("交叉编译", result.stdout + repeated.stdout)

    def test_unused_obsolete_native_sdk_does_not_block_normal_configuration(self):
        result = self.run_controller("init", extra_environment={
            "HETERO_OPENHARMONY_NATIVE_SDK_ROOT": str(self.root / "removed-sdk"),
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_optional_install_asset_failure_does_not_stop_core_install(self):
        controller = CONTROLLER.read_text()
        start = controller.index("prepare_optional_pmu_sampler() {")
        end = controller.index("\nprovision_pmu_sampler() {", start)
        helper = controller[start:end]
        shell = "set -eu\nDEPLOYMENT_MODE=device\nAPP_ROOT=/platform\nRUNTIME_ROOT=/runtime\nprovision_pmu_sampler_bundle() { return 2; }\n" + helper + "\nprepare_optional_pmu_sampler\nprintf 'CORE_CONTINUES\\n'\n"
        result = subprocess.run(["bash", "-c", shell], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CORE_CONTINUES", result.stdout)
        self.assertIn("PMU 采集器自动准备失败", result.stderr)
        self.assertIn("heteroctl pmu", result.stderr)


class PmuDeploymentLifecycleTests(unittest.TestCase):
    """Execute real lifecycle functions with service/dependency boundaries stubbed.

    Provisioning itself runs its real CLI and reads the committed bundle. No
    production runtime, network, compiler, HDC or device operation is invoked.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pmu-lifecycle-")
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / "app"
        self.runtime = self.root / "runtime"
        self.asset = self.runtime / "assets/pmu-sampler"
        self.log = self.root / "operations.log"
        for directory in (self.app / ".venv/bin", self.app / "node_modules/.bin", self.app / ".next"):
            directory.mkdir(parents=True)
        shim = self.app / ".venv/bin/python"
        shim.write_text(
            f"#!{sys.executable}\nimport os,sys\n"
            "if sys.argv[1:3] == ['-m', 'backend.tools.validate_next_build']: sys.exit(0)\n"
            "assert sys.argv[1:3] == ['-m', 'tools.scenario_capture.provision_pmu_sampler'], sys.argv\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
        )
        shim.chmod(0o755)
        next_bin = self.app / "node_modules/.bin/next"
        next_bin.write_text("#!/bin/sh\nexit 93\n")
        next_bin.chmod(0o755)
        (self.app / ".next/BUILD_ID").write_text("isolated-fixture")

    def tearDown(self):
        self.temporary.cleanup()

    def run_lifecycle(self, command, mode="device", obsolete_sdk=False):
        source = CONTROLLER.read_text()
        names = ("provision_pmu_sampler_bundle", "prepare_optional_pmu_sampler", "install_runtime", "start_runtime")
        definitions = []
        for name in names:
            match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", source, re.MULTILINE | re.DOTALL)
            self.assertIsNotNone(match, name)
            definitions.append(match.group(0))
        values = {
            "APP_ROOT": self.app, "RUNTIME_ROOT": self.runtime,
            "LOG_DIR": self.runtime / "logs", "PID_DIR": self.runtime / "run",
            "CACHE_DIR": self.runtime / "cache", "TMP_DIR": self.runtime / "tmp",
            "ENV_FILE": self.runtime / "platform.env", "DEPLOYMENT_MODE": mode,
            "HETERO_PMU_SAMPLER_BUNDLE_ROOT": self.asset, "HETERO_KIMI_BIN": self.root / "no-kimi",
            "HETERO_OPENHARMONY_NATIVE_SDK_ROOT": self.root / "removed-sdk" if obsolete_sdk else "",
            "HETERO_ACCESS_MODE": "local", "COMMAND": command,
            "SKIP_PYTHON_DEPS": 1, "SKIP_WEB_DEPS": 1, "SKIP_KIMI": 1, "SKIP_BUILD": 1,
            "SDK_SOURCE_ROOT": "", "REQUESTED_ACCESS_MODE": "", "REQUESTED_WEB_PORT": "",
        }
        assignments = "\n".join(f"{key}={shlex.quote(str(value))}" for key, value in values.items())
        empty = (
            "resolve_python_bin require_command check_python_version check_node_version "
            "initialize_runtime load_environment validate_environment_paths active_app_runtime_processes "
            "validate_managed_virtualenv skip_install_stage validate_agent_cli_configuration "
            "check_deployment_resources make_runtime_root set_paths validate_planned_paths "
            "normalize_requested_resource_overrides stale_web_build_source prepare_web_listener "
            "prepare_api_listener cleanup_stale_agent_cli_processes wait_ready"
        ).split()
        stubs = "\n".join(f"{name}() {{ :; }}" for name in empty)
        stubs += "\npid_alive() { return 1; }\nrun_install_stage() { shift 2; \"$@\"; }\n"
        stubs += f"run_install_operation() {{ printf '%s\\n' \"$1\" >> {shlex.quote(str(self.log))}; shift; \"$@\"; }}\n"
        stubs += f"start_owned_process() {{ printf 'start %s\\n' \"$1\" >> {shlex.quote(str(self.log))}; }}\n"
        stubs += f"stop_runtime() {{ printf 'stop\\n' >> {shlex.quote(str(self.log))}; }}\n"
        stubs += "startup_positive_integer() { printf '%s\\n' \"$2\"; }\n"
        shell = "set -euo pipefail\n" + assignments + "\n" + "\n".join(definitions) + "\n" + stubs
        shell += source[source.rindex('case "$COMMAND" in'):]
        environment = {key: value for key, value in os.environ.items() if not key.startswith("HETERO_")}
        environment["PYTHONPATH"] = str(ROOT)
        return subprocess.run(["bash", "-c", shell], cwd=self.root, env=environment,
                              capture_output=True, text=True, timeout=20)

    def test_normal_install_start_and_restart_provision_without_sdk(self):
        for command in ("install", "start", "restart"):
            with self.subTest(command=command):
                # Each lifecycle command must independently prepare a fresh destination.
                self.asset = self.runtime / "assets" / command
                result = self.run_lifecycle(command, obsolete_sdk=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(verify_bundle(self.asset)["target"], TARGET)
        operations = self.log.read_text()
        self.assertEqual(operations.count("校验并准备仓库自带 PMU 采集器"), 3)
        self.assertIn("stop\n", operations)

    def test_repeated_start_reuses_bundle_without_rewriting_manifest(self):
        first = self.run_lifecycle("start")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        manifest = self.asset / "manifest.json"
        timestamp = manifest.stat().st_mtime_ns
        second = self.run_lifecycle("start")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(manifest.stat().st_mtime_ns, timestamp)
        self.assertEqual(verify_bundle(self.asset)["target"], TARGET)

    def test_analysis_lifecycle_never_prepares_pmu_assets(self):
        for command in ("install", "start", "restart"):
            with self.subTest(command=command):
                result = self.run_lifecycle(command, mode="analysis")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(self.asset.exists())
        self.assertNotIn("PMU", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
