from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as platform_app
from backend import device_verification as device
from backend.core import ArtifactStore, PlatformError


WEB_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = WEB_ROOT
SOURCE_BINDING = {
    "declared_revision": None,
    "resolved_snapshot_id": "snapshot_test",
    "match_level": "unknown",
    "resolver_version": "1.0",
}


def write_fake_hdc(directory: Path) -> Path:
    path = directory / "hdc"
    path.write_text(
        """#!/bin/sh
if [ "$1" = "list" ] && [ "$2" = "targets" ]; then
  echo "test-device-01"
  exit 0
fi
if [ "$1" = "version" ]; then
  echo "Ver: fake-hdc-1.0"
  exit 0
fi
if [ "$1" = "-t" ]; then
  serial="$2"
  shift 2
  if [ "$serial" != "test-device-01" ]; then exit 1; fi
  if [ "$1" = "shell" ] && [ "$2" = "param" ] && [ "$3" = "get" ]; then
    case "$4" in
      const.product.model) echo "TestPhone" ;;
      ohos.boot.hardware|ohos.boot.chiptype) echo "kirin-test" ;;
      const.gpu.vendor) echo "testgpu.v1" ;;
      const.ohos.fullname) echo "OpenHarmony-test" ;;
      const.ohos.apiversion) echo "26" ;;
      const.SystemCapability.Graphic.Graphic2D.GLES3) echo "true" ;;
      const.SystemCapability.Graphic.Graphic2D.Vulkan) echo "true" ;;
    esac
    exit 0
  fi
  if [ "$1" = "shell" ] && [ "$2" = "ls" ]; then
    case "$3" in
      /system/lib64/libEGL.so|/system/lib64/libGLESv3.so|/system/lib64/libvulkan.so|/vendor/lib64/libspeaker_processing_dsp.z.so|/system/lib64/ndk/libneural_network_core.so|/system/lib64/ndk/libneural_network_runtime.so)
        echo "$3"; exit 0 ;;
      *) exit 1 ;;
    esac
  fi
  if [ "$1" = "shell" ] && [ "$2" = "rm" ]; then exit 0; fi
  if [ "$1" = "shell" ] && [ "$2" = "echo remote-ok" ]; then echo "remote-ok"; exit 0; fi
  if [ "$1" = "shell" ] && [ "$2" = "exit 7" ]; then echo "remote-fail" >&2; exit 7; fi
  if [ "$1" = "shell" ] && [ "$2" = "flood-output" ]; then
    i=0
    while [ "$i" -lt 700 ]; do
      printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
      i=$((i + 1))
    done
    exit 0
  fi
fi
exit 1
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class DeviceVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.hdc = write_fake_hdc(Path(self.tempdir.name))
        self.environment = patch.dict(os.environ, {"HETERO_HDC_BIN": str(self.hdc)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.probe = device.probe_device(WORKSPACE_ROOT, "test-device-01")
        self.probe["artifact_id"] = "art_111111111111111111111111"
        code = (WEB_ROOT / device.GPU_BRIGHTNESS_SAMPLE).read_text(encoding="utf-8")
        self.code_artifact = {
            "artifact_id": "art_222222222222222222222222",
            "source_connector_id": "connector_test",
            "source_binding": SOURCE_BINDING,
            "code": code,
        }
        self.npu_code_artifact = {
            **self.code_artifact,
            "artifact_id": "art_555555555555555555555555",
            "code": (WEB_ROOT / device.NPU_MATMUL_SAMPLE).read_text(encoding="utf-8"),
        }
        self.reasoning_result = {
            "artifact_id": "art_333333333333333333333333",
            "strategy_id": "hardware_kb",
            "result": {"recommendation": "evidence_required", "evidence_status": "model_hypothesis", "confidence": "low"},
        }

    def test_readonly_probe_discovers_observed_gpu_and_dsp_without_claiming_support(self) -> None:
        discovery = device.discover_devices(WORKSPACE_ROOT)

        self.assertTrue(discovery["hdc"]["available"])
        self.assertEqual(discovery["devices"][0]["serial"], "test-device-01")
        self.assertEqual(self.probe["device"]["model"], "TestPhone")
        self.assertEqual(self.probe["observed_units"]["gpu"]["observation_status"], "observed")
        self.assertIn("GLES3", self.probe["observed_units"]["gpu"]["observed_capabilities"])
        self.assertEqual(self.probe["observed_units"]["dsp"]["observation_status"], "observed")
        self.assertIn("不等价", self.probe["claim_boundary_zh"])

    def test_bounded_discovery_uses_one_target_query_without_version_probe(self) -> None:
        with patch.object(device, "_run_hdc", wraps=device._run_hdc) as command:
            discovery = device.discover_devices(WORKSPACE_ROOT, timeout_seconds=5)
        self.assertEqual(discovery["devices"], [{"serial": "test-device-01", "connection_state": "connected"}])
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[1], ("list", "targets"))
        self.assertEqual(command.call_args.kwargs["timeout_seconds"], 5)
        for value in (0, True, "5"):
            with self.assertRaises(PlatformError) as caught:
                device.discover_devices(WORKSPACE_ROOT, timeout_seconds=value)
            self.assertEqual(caught.exception.code, "DEVICE_DISCOVERY_TIMEOUT_INVALID")

    def test_full_access_remote_shell_is_bound_to_probe_and_never_uses_a_host_shell(self) -> None:
        result = device.execute_remote_device_shell(
            WORKSPACE_ROOT,
            probe_artifact=self.probe,
            command="echo remote-ok",
            timeout_seconds=30,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["device"]["serial"], "test-device-01")
        self.assertEqual(result["execution"]["stdout"], "remote-ok")
        self.assertFalse(result["workstation_execution_boundary"]["host_shell"])
        self.assertEqual(result["workstation_execution_boundary"]["device_serial_source"], "previously_probed_artifact")

    def test_remote_shell_keeps_device_nonzero_exit_as_audited_result(self) -> None:
        result = device.execute_remote_device_shell(
            WORKSPACE_ROOT,
            probe_artifact=self.probe,
            command="exit 7",
            timeout_seconds=30,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["execution"]["return_code"], 7)
        self.assertEqual(result["execution"]["stderr"], "remote-fail")

    def test_remote_shell_drains_and_bounds_large_device_output(self) -> None:
        result = device.execute_remote_device_shell(
            WORKSPACE_ROOT,
            probe_artifact=self.probe,
            command="flood-output",
            timeout_seconds=30,
        )

        stdout = result["execution"]["stdout"]
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("[输出已截断]", stdout)
        self.assertLessEqual(len(stdout.encode("utf-8")), device.MAX_COMMAND_OUTPUT_BYTES)

    def test_gpu_reference_source_creates_an_executable_but_confirmed_plan(self) -> None:
        plan = device.build_validation_plan(
            WORKSPACE_ROOT,
            probe_artifact=self.probe,
            code_artifact=self.code_artifact,
            reasoning_result_artifact=self.reasoning_result,
            requested_unit="gpu",
        )

        self.assertEqual(plan["plan_status"], "ready_for_user_confirmation")
        self.assertEqual(plan["recipe_contract"]["recipe_id"], "gpu_gles3_rgba_brightness_smoke/v1")
        self.assertTrue(plan["confirmation"]["required"])
        self.assertTrue(plan["confirmation"]["nonce"])

    def test_unknown_hotspot_requires_a_model_dispatch_assessment_before_any_recipe(self) -> None:
        altered_code = {**self.code_artifact, "code": "void Other() { for (int i = 0; i < 4; ++i) {} }"}
        plan = device.build_validation_plan(
            WORKSPACE_ROOT,
            probe_artifact=self.probe,
            code_artifact=altered_code,
            reasoning_result_artifact=self.reasoning_result,
            requested_unit="gpu",
        )

        self.assertEqual(plan["plan_status"], "evidence_required")
        self.assertIsNone(plan["recipe_contract"])
        self.assertIn("模型下发判断", plan["planning_reason_zh"])

    def test_model_semantic_candidate_uses_a_baseline_without_matching_source_text(self) -> None:
        altered_code = {**self.code_artifact, "code": "void BrandNewImageKernel(float *value) { value[0] *= 1.1F; }"}
        assessment = {
            "artifact_id": "art_999999999999999999999999",
            "result": {
                "decision": "semantic_candidate",
                "target_unit": "gpu",
                "execution_mode": "gles_fragment_pipeline",
                "summary": "模型识别到可见像素变换核心，可形成 GPU 下发合同。",
                "dispatch_contract": {
                    "semantic_operations": ["逐像素亮度变换"],
                    "input_assumptions": ["输入格式待 adapter 明确"],
                    "required_adapter_work": ["建立 RGBA buffer adapter"],
                    "cpu_fallback": ["保留当前 CPU 路径"],
                    "result_comparison": ["固定容差像素比较"],
                },
                "required_device_checks": ["验证目标格式与 context"],
                "validation": {"status": "valid", "errors": []},
            },
        }
        plan = device.build_validation_plan(
            WORKSPACE_ROOT,
            probe_artifact=self.probe,
            code_artifact=altered_code,
            reasoning_result_artifact=self.reasoning_result,
            requested_unit="gpu",
            dispatch_assessment_artifact=assessment,
        )

        self.assertEqual(plan["plan_status"], "ready_for_user_confirmation")
        self.assertEqual(plan["recipe_contract"]["recipe_scope"], "platform_baseline_preflight")
        self.assertNotIn("reference_match_level", plan["recipe_contract"])
        self.assertIn("不编译或替换当前热点代码", plan["recipe_contract"]["execution_scope_zh"])
        self.assertEqual(plan["dispatch_assessment"]["decision"], "semantic_candidate")

    def test_invalid_model_contract_cannot_open_a_recipe(self) -> None:
        assessment = {
            "artifact_id": "art_aaaaaaaaaaaaaaaaaaaaaaaa",
            "result": {
                "decision": "semantic_candidate",
                "target_unit": "npu",
                "execution_mode": "nnrt_graph",
                "summary": "缺少合同字段的候选。",
                "validation": {"status": "invalid", "errors": ["dispatch_contract.semantic_operations 不能为空"]},
            },
        }
        plan = device.build_validation_plan(
            WORKSPACE_ROOT,
            probe_artifact=self.probe,
            code_artifact=self.npu_code_artifact,
            reasoning_result_artifact=self.reasoning_result,
            requested_unit="npu",
            dispatch_assessment_artifact=assessment,
        )

        self.assertEqual(plan["plan_status"], "evidence_required")
        self.assertIsNone(plan["recipe_contract"])
        self.assertIn("未通过校验", plan["planning_reason_zh"])

    def test_exact_npu_matmul_creates_a_confirmed_capability_preflight(self) -> None:
        plan = device.build_validation_plan(
            WORKSPACE_ROOT,
            probe_artifact=self.probe,
            code_artifact=self.npu_code_artifact,
            reasoning_result_artifact=self.reasoning_result,
            requested_unit="npu",
        )

        self.assertEqual(plan["plan_status"], "ready_for_user_confirmation")
        self.assertEqual(plan["recipe_contract"]["recipe_id"], "npu_nnrt_matmul_capability_preflight/v1")
        self.assertIn("OH_NN_ACCELERATOR", plan["recipe_contract"]["execution_scope_zh"])

    def test_terminal_newline_only_difference_keeps_the_registered_npu_reference_match(self) -> None:
        # Textarea and fenced-code input can drop only the final newline. This
        # must not turn an otherwise identical reviewed reference into an
        # arbitrary user program.
        newline_trimmed = {**self.npu_code_artifact, "code": self.npu_code_artifact["code"].rstrip("\n")}
        plan = device.build_validation_plan(
            WORKSPACE_ROOT,
            probe_artifact=self.probe,
            code_artifact=newline_trimmed,
            reasoning_result_artifact=self.reasoning_result,
            requested_unit="npu",
        )

        self.assertEqual(plan["plan_status"], "ready_for_user_confirmation")
        self.assertEqual(plan["recipe_contract"]["reference_match_level"], "terminal_newline_normalized")

    def test_npu_preflight_records_an_unsupported_operator_as_evidence(self) -> None:
        plan = device.build_validation_plan(
            WORKSPACE_ROOT,
            probe_artifact=self.probe,
            code_artifact=self.npu_code_artifact,
            reasoning_result_artifact=self.reasoning_result,
            requested_unit="npu",
        )
        plan["artifact_id"] = "art_666666666666666666666666"
        completed = subprocess.CompletedProcess(
            args=["bash", "registered-npu-runner"],
            returncode=1,
            stdout=(
                "NPU NNRT device enumeration: PASS\n"
                "NPU NNRT selected accelerator:\n"
                "NPU_kirin_test\n"
                "NPU NNRT graph: FINISH PASS\n"
                "NPU NNRT MatMul availability: NOT_SUPPORTED\n"
                "NPU NNRT MatMul accelerator offload: FAIL\n"
            ),
            stderr="",
        )
        with patch.object(device, "_run_registered_npu_recipe", return_value=completed):
            result = device.execute_validation_plan(
                WORKSPACE_ROOT,
                Path(self.tempdir.name),
                plan_artifact=plan,
                user_confirmed=True,
                confirmation_nonce=str(plan["confirmation"]["nonce"]),
            )

        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["observed_evidence"]["accelerator_name"], "NPU_kirin_test")
        self.assertEqual(result["observed_evidence"]["operation_availability"], "not_supported")
        self.assertIn("明确拒绝", result["summary_zh"])

    def test_runner_requires_confirmation_and_calibrates_success(self) -> None:
        plan = device.build_validation_plan(
            WORKSPACE_ROOT,
            probe_artifact=self.probe,
            code_artifact=self.code_artifact,
            reasoning_result_artifact=self.reasoning_result,
            requested_unit="gpu",
        )
        plan["artifact_id"] = "art_444444444444444444444444"

        with self.assertRaises(PlatformError) as raised:
            device.execute_validation_plan(
                WORKSPACE_ROOT,
                Path(self.tempdir.name),
                plan_artifact=plan,
                user_confirmed=False,
                confirmation_nonce="",
            )
        self.assertEqual(raised.exception.code, "DEVICE_VALIDATION_CONFIRMATION_REQUIRED")

        completed = subprocess.CompletedProcess(
            args=["bash", "registered-runner"],
            returncode=0,
            stdout="GPU offload renderer:\nTest GPU\nGPU API version:\nOpenGL ES 3.2\nGPU RGBA brightness offload: PASS\n",
            stderr="",
        )
        with patch.object(device, "_run_registered_gpu_recipe", return_value=completed):
            result = device.execute_validation_plan(
                WORKSPACE_ROOT,
                Path(self.tempdir.name),
                plan_artifact=plan,
                user_confirmed=True,
                confirmation_nonce=str(plan["confirmation"]["nonce"]),
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["observed_evidence"]["renderer"], "Test GPU")
        self.assertTrue(result["cleanup"]["attempted"])
        self.assertIn("不会自动提升", result["evidence_calibration"]["recommendation_effect"])

    def test_plan_api_requires_the_reasoning_context_to_reference_current_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            arguments = {
                "source_connector_id": "connector_test",
                "source_binding": SOURCE_BINDING,
                "producer": {"name": "test", "version": "1.0.0"},
            }
            probe = store.write("device_probe", self.probe, **arguments)
            code = store.write("hotspot_code_unit", self.code_artifact, **arguments)
            unrelated_code = store.write(
                "hotspot_code_unit",
                {**self.code_artifact, "code": "void Unrelated() {}"},
                **arguments,
            )
            context = store.write(
                "reasoning_context_pack",
                {
                    "strategy_id": "hardware_kb",
                    "input_artifacts": [f"artifact:{unrelated_code['artifact_id']}"],
                },
                **arguments,
            )
            result = store.write(
                "opportunity_reasoning_result",
                {
                    "strategy_id": "hardware_kb",
                    "context_artifact_id": context["artifact_id"],
                    "result": self.reasoning_result["result"],
                },
                **arguments,
                input_artifacts=[f"artifact:{context['artifact_id']}"]
            )

            request = platform_app.DeviceValidationPlanRequest(
                device_probe_artifact_id=str(probe["artifact_id"]),
                hotspot_code_artifact_id=str(code["artifact_id"]),
                reasoning_result_artifact_id=str(result["artifact_id"]),
                requested_unit="gpu",
            )
            with patch.object(platform_app, "store", store):
                with self.assertRaises(PlatformError) as raised:
                    platform_app.create_device_validation_plan("local-default", request)

        self.assertEqual(raised.exception.code, "DEVICE_VALIDATION_ANALYSIS_MISMATCH")

    def test_full_access_agent_authorization_only_runs_an_existing_registered_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            arguments = {
                "source_connector_id": "connector_test",
                "source_binding": SOURCE_BINDING,
                "producer": {"name": "test", "version": "1.0.0"},
            }
            plan = store.write(
                "device_validation_plan",
                {
                    "confirmation": {"required": True, "nonce": "registered-nonce"},
                    "device": {"serial": "test-device-01"},
                    "plan_status": "ready_for_user_confirmation",
                },
                **arguments,
            )
            with patch.object(platform_app, "store", store), patch.object(
                platform_app,
                "execute_validation_plan",
                return_value={"status": "passed", "summary_zh": "已运行登记配方。"},
            ) as execute:
                response = platform_app.run_device_validation(
                    "local-default",
                    platform_app.DeviceValidationRunRequest(
                        plan_artifact_id=str(plan["artifact_id"]),
                        execution_authorization="agent_full_access",
                    ),
                )

            self.assertEqual(execute.call_args.kwargs["user_confirmed"], True)
            self.assertEqual(execute.call_args.kwargs["confirmation_nonce"], "registered-nonce")
            self.assertEqual(response["artifact"]["execution_authorization"], "agent_full_access")

    def test_remote_shell_api_only_accepts_probe_bound_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            arguments = {
                "source_connector_id": "connector_test",
                "source_binding": SOURCE_BINDING,
                "producer": {"name": "test", "version": "1.0.0"},
            }
            probe = store.write("device_probe", self.probe, **arguments)
            payload = {
                "schema_version": "device_remote_shell_run/v1",
                "probe_artifact_id": probe["artifact_id"],
                "device": {"serial": "test-device-01"},
                "command": "echo remote-ok",
                "command_sha256": "b" * 64,
                "status": "succeeded",
                "execution": {"return_code": 0, "timed_out": False, "stdout": "remote-ok", "stderr": ""},
                "workstation_execution_boundary": {"host_shell": False},
                "summary_zh": "远端命令完成。",
            }
            with patch.object(platform_app, "store", store), patch.object(
                platform_app,
                "execute_remote_device_shell",
                return_value=payload,
            ) as execute:
                response = platform_app.run_device_shell(
                    "local-default",
                    platform_app.DeviceRemoteShellRunRequest(
                        device_probe_artifact_id=str(probe["artifact_id"]),
                        command="echo remote-ok",
                        timeout_seconds=45,
                        execution_authorization="agent_full_access",
                    ),
                )

            self.assertEqual(execute.call_args.kwargs["probe_artifact"].get("artifact_id"), probe["artifact_id"])
            self.assertEqual(execute.call_args.kwargs["command"], "echo remote-ok")
            self.assertEqual(execute.call_args.kwargs["timeout_seconds"], 45)
            self.assertEqual(response["artifact"]["execution_authorization"], "agent_full_access")
