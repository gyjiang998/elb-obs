from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.tools.deployment_preflight import (
    _clang_resource_headers_check,
    _load_environment_file,
    _sandbox_check,
    collect_checks,
    collect_resource_checks,
)


class DeploymentPreflightTests(unittest.TestCase):
    def test_analysis_mode_skips_device_and_trace_resource_inspection(self) -> None:
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "analysis"}), \
                patch("backend.tools.deployment_preflight._sdk_header_check") as sdk, \
                patch("backend.tools.deployment_preflight.resolve_hdc_binary") as hdc:
            checks = collect_checks()
        self.assertTrue(checks)
        self.assertTrue(all(item["capability"] == "core" for item in checks))
        sdk.assert_not_called()
        hdc.assert_not_called()

    def test_device_mode_reports_resource_configuration_and_windows_bridge_option(self) -> None:
        from backend.core import PlatformError

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "HETERO_DEPLOYMENT_MODE": "device",
            "HETERO_TRACE_STREAMER": str(Path(temp) / "missing-streamer"),
            "HETERO_RUNTIME_ASSET_ROOT": temp,
            "HETERO_STATE_ROOT": temp,
            "HETERO_RUNTIME_LIBRARY_ROOT": temp,
        }), patch("backend.tools.deployment_preflight.resolve_hdc_binary", side_effect=PlatformError("DEVICE_HDC_UNAVAILABLE", "missing")):
            checks = {row["check_id"]: row for row in collect_resource_checks()}
        self.assertEqual(checks["trace.streamer"]["status"], "unavailable")
        self.assertIn("HETERO_TRACE_STREAMER", checks["trace.streamer"]["detail"])
        self.assertIn("Windows", checks["device.hdc"]["detail"])
        self.assertEqual(checks["device.hdc"]["capability"], "connection")
        self.assertEqual(checks["runtime.gpu_gles3"]["status"], "unavailable")

    def test_worker_checks_trace_and_build_resources_without_server_device_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "HETERO_DEPLOYMENT_MODE": "worker",
            "HETERO_TRACE_STREAMER": str(Path(temp) / "missing-streamer"),
            "HETERO_RUNTIME_ASSET_ROOT": temp,
            "HETERO_STATE_ROOT": temp,
            "HETERO_RUNTIME_LIBRARY_ROOT": temp,
        }), patch("backend.tools.deployment_preflight._sdk_header_check", return_value=(False, "missing")), \
                patch("backend.tools.deployment_preflight.resolve_hdc_binary") as hdc, \
                patch("backend.tools.deployment_preflight.verify_bundle") as pmu:
            checks = collect_resource_checks()
        self.assertIn("trace.streamer", {row["check_id"] for row in checks})
        self.assertIn("toolchain.clang", {row["check_id"] for row in checks})
        self.assertTrue(all(row["capability"] in {"trace", "build"} for row in checks))
        self.assertNotIn("device.hdc", {row["check_id"] for row in checks})
        self.assertNotIn("pmu.bundle", {row["check_id"] for row in checks})
        hdc.assert_not_called()
        pmu.assert_not_called()

    def test_clang_resource_headers_are_part_of_build_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            llvm_bin = Path(temp) / "toolchain" / "bin"
            llvm_bin.mkdir(parents=True)
            ready, inspected, detail = _clang_resource_headers_check(llvm_bin)
            self.assertFalse(ready)
            self.assertEqual(inspected, llvm_bin.parent / "lib" / "clang")
            self.assertIn("stddef.h", detail)

            headers = llvm_bin.parent / "lib" / "clang" / "18" / "include"
            headers.mkdir(parents=True)
            (headers / "stddef.h").write_text("typedef int size_t;\n", encoding="utf-8")
            (headers / "stdint.h").write_text("typedef int int32_t;\n", encoding="utf-8")
            ready, inspected, _ = _clang_resource_headers_check(llvm_bin)
            self.assertTrue(ready)
            self.assertEqual(inspected, headers)

    def test_worker_core_preflight_omits_web_and_server_agent_gates(self) -> None:
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "worker"}), \
                patch("backend.tools.deployment_preflight.collect_resource_checks", return_value=[]), \
                patch("backend.tools.deployment_preflight.validate_next_build") as next_build, \
                patch("backend.tools.deployment_preflight.validate_curated_template_catalog") as templates:
            checks = collect_checks()
        self.assertEqual({row["check_id"] for row in checks}, {"state.writable", "source.default", "analysis.tools"})
        next_build.assert_not_called()
        templates.assert_not_called()

    def test_worker_require_build_uses_build_capability_and_rejects_device_requirement(self) -> None:
        rows = [{"check_id": "toolchain.clang", "status": "unavailable", "capability": "build", "path": "missing", "detail": "configure"}]
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "worker"}), \
                patch("backend.tools.deployment_preflight.collect_resource_checks", return_value=rows), \
                patch("builtins.print"):
            with patch.object(sys, "argv", ["preflight", "--resources-only", "--require-build"]):
                from backend.tools import deployment_preflight
                self.assertEqual(deployment_preflight.main(), 1)
            with patch.object(sys, "argv", ["preflight", "--resources-only", "--require-device"]), self.assertRaises(SystemExit) as raised:
                deployment_preflight.main()
            self.assertEqual(raised.exception.code, 2)

    def test_environment_file_is_loaded_without_shell_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "platform.env"
            path.write_text(
                "# deployment values\nHETERO_TEST_PLAIN=value\nHETERO_TEST_QUOTED=\"quoted value\"\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                _load_environment_file(path)
                self.assertEqual(os.environ["HETERO_TEST_PLAIN"], "value")
                self.assertEqual(os.environ["HETERO_TEST_QUOTED"], "quoted value")

    def test_environment_file_rejects_invalid_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "platform.env"
            path.write_text("BAD-KEY=value\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid environment key"):
                _load_environment_file(path)

    def test_required_network_sandbox_needs_executable_and_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            wrapper = Path(temporary_directory) / "sandbox"
            wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            wrapper.chmod(0o755)
            with patch.dict(
                os.environ,
                {
                    "HETERO_BUILD_REQUIRE_NETWORK_SANDBOX": "1",
                    "HETERO_BUILD_SANDBOX_PREFIX": str(wrapper),
                    "HETERO_BUILD_SANDBOX_NETWORK_ISOLATED": "1",
                },
                clear=False,
            ):
                ready, path, _ = _sandbox_check()
            self.assertTrue(ready)
            self.assertEqual(path, wrapper)

    def test_unwritable_state_root_is_reported_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "not-a-directory"
            state_file.write_text("occupied", encoding="utf-8")
            with patch.dict(os.environ, {"HETERO_STATE_ROOT": str(state_file)}, clear=False):
                checks = collect_checks()
            state_check = next(item for item in checks if item["check_id"] == "state.writable")
            self.assertEqual(state_check["status"], "unavailable")

    def test_curated_agent_template_catalog_is_a_core_deployment_asset(self) -> None:
        checks = collect_checks()
        template_check = next(item for item in checks if item["check_id"] == "agent.templates")

        self.assertEqual(template_check["capability"], "core")
        self.assertEqual(template_check["status"], "ready")

    def test_effective_agent_tool_policy_is_a_core_deployment_check(self) -> None:
        checks = collect_checks()
        policy_check = next(item for item in checks if item["check_id"] == "agent.external-search-disabled")

        self.assertEqual(policy_check["capability"], "core")
        self.assertEqual(policy_check["status"], "ready")


if __name__ == "__main__":
    unittest.main()
