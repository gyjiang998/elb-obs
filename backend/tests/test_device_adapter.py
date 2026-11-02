from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend import device_adapter as adapter
from backend.core import PlatformError


WEB_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = WEB_ROOT


class DeviceAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.code = {
            "artifact_id": "art_111111111111111111111111",
            "source_connector_id": "connector_test",
            "source_binding": {"match_level": "unknown"},
            "code": "void Brightness(float *out, const float *in, int count) { for (int i = 0; i < count; ++i) out[i] = in[i] + 0.25F; }",
            "source": {"origin": "user_paste", "path": "pasted.cpp", "sha256": "a" * 64, "line_start": 1, "line_end": 1},
        }
        self.probe = {
            "artifact_id": "art_222222222222222222222222",
            "observed_units": {
                "gpu": {"observation_status": "observed", "observed_capabilities": ["GLES3"]},
                "npu": {"observation_status": "observed", "observed_capabilities": ["NNRT runtime"]},
            },
        }

    def _assessment(self, unit: str) -> dict[str, object]:
        return {
            "artifact_id": "art_333333333333333333333333",
            "result": {
                "decision": "semantic_candidate",
                "target_unit": unit,
                "execution_mode": "registered_adapter_candidate",
                "summary": "可见的最小计算作用域可用于受控 Adapter 验证。",
                "reasoning": "存在独立的计算核心，仍需固定输入输出验证。",
                "candidate_scope": {"level": "loop_region", "description": "可见逐元素或矩阵计算区域"},
                "observed_code_evidence": ["可见循环和连续数值输入输出。"],
                "dispatch_contract": {
                    "semantic_operations": ["受控计算核心"],
                    "input_assumptions": ["输入布局由 Adapter 固定"],
                    "output_semantics": ["与 CPU fallback 对照"],
                    "state_and_side_effects": ["固定测试向量无外部副作用"],
                    "required_adapter_work": ["构建受控图或 shader"],
                    "cpu_fallback": ["保留 CPU 参考实现"],
                    "result_comparison": ["固定向量结果比较"],
                },
                "validation": {"status": "valid", "errors": []},
                "evidence_ids": ["hardware:registered"],
            },
        }

    def _context(self, unit: str) -> dict[str, object]:
        return adapter.build_device_adapter_context(
            code_artifact=self.code,
            dispatch_assessment_artifact=self._assessment(unit),
            probe_artifact=self.probe,
            requested_unit=unit,
        )

    def _gpu_spec(self, context: dict[str, object]) -> dict[str, object]:
        allowed = context["model_input_context"]["allowed_evidence_ids"]
        return {
            "schema_version": "device_adapter_spec/v1",
            "decision": "ready_for_materialization",
            "target_unit": "gpu",
            "adapter_kind": adapter.GPU_ADAPTER_KIND,
            "summary": "为固定两像素亮度变换生成 GLES3 Adapter。",
            "rationale": "当前模板与逐元素亮度叠加语义一致。",
            "source_scope": {"level": "loop_region", "description": "逐像素亮度叠加"},
            "implementation": {
                "fragment_expression": "vec4(clamp(pixel.rgb + vec3(brightness), 0.0, 1.0), pixel.a)",
                "brightness": 0.25,
                "input_rgba": [0, 64, 128, 255, 191, 10, 200, 17],
            },
            "verification_plan": ["固定输入的 GPU readback 与平台 CPU 参考比较。"],
            "risks": ["只覆盖固定输入与已登记语义。"],
            "evidence_ids": allowed[:2],
        }

    def test_gpu_reference_is_computed_by_platform_and_source_is_materialized(self) -> None:
        context = self._context("gpu")
        normalized, errors = adapter.validate_device_adapter_spec(self._gpu_spec(context), context)

        self.assertEqual(errors, [])
        self.assertEqual(normalized["validation"]["status"], "valid")
        self.assertEqual(normalized["implementation"]["expected_rgba"], [64, 128, 192, 255, 255, 74, 255, 17])
        materialized = adapter.materialize_device_adapter(
            WORKSPACE_ROOT,
            Path(self.tempdir.name),
            context=context,
            spec=normalized,
        )
        self.assertEqual(materialized["status"], "ready")
        source = Path(self.tempdir.name, materialized["source_relative_path"]).read_text(encoding="utf-8")
        self.assertIn("HETERO_GPU_ADAPTER_PASS", source)
        self.assertIn("vec4(clamp(pixel.rgb+vec3(brightness),0.0,1.0),pixel.a)", source)
        self.assertIn("64, 128, 192, 255", source)

    def test_gpu_unregistered_expression_is_downgraded_without_source_generation(self) -> None:
        context = self._context("gpu")
        spec = self._gpu_spec(context)
        spec["implementation"]["fragment_expression"] = "vec4(pixel.bgr, pixel.a)"

        normalized, errors = adapter.validate_device_adapter_spec(spec, context)

        self.assertTrue(errors)
        self.assertEqual(normalized["decision"], "needs_contract")
        materialized = adapter.materialize_device_adapter(
            WORKSPACE_ROOT,
            Path(self.tempdir.name),
            context=context,
            spec=normalized,
        )
        self.assertEqual(materialized["status"], "evidence_required")

    def test_npu_matmul_uses_backend_cpu_reference(self) -> None:
        context = self._context("npu")
        allowed = context["model_input_context"]["allowed_evidence_ids"]
        spec = {
            "schema_version": "device_adapter_spec/v1",
            "decision": "ready_for_materialization",
            "target_unit": "npu",
            "adapter_kind": adapter.NPU_ADAPTER_KIND,
            "summary": "为固定 FLOAT32 MatMul 生成 NNRT Adapter。",
            "rationale": "合同与二维 MatMul 模板一致。",
            "source_scope": {"level": "loop_region", "description": "二维乘加核心"},
            "implementation": {
                "operation": "matmul",
                "left_shape": [2, 3],
                "right_shape": [3, 2],
                "left_values": [1, 2, 3, 4, 5, 6],
                "right_values": [7, 8, 9, 10, 11, 12],
                "tolerance": 0.0001,
            },
            "verification_plan": ["在选定 NNRT 加速器执行并与 CPU 参考比较。"],
            "risks": ["精确图可能被设备拒绝。"],
            "evidence_ids": allowed[:2],
        }

        normalized, errors = adapter.validate_device_adapter_spec(spec, context)

        self.assertEqual(errors, [])
        self.assertEqual(normalized["implementation"]["cpu_reference_output"], [58.0, 64.0, 139.0, 154.0])

    def test_changed_materialized_source_is_rejected_before_hdc_execution(self) -> None:
        context = self._context("gpu")
        normalized, errors = adapter.validate_device_adapter_spec(self._gpu_spec(context), context)
        self.assertEqual(errors, [])
        state_root = Path(self.tempdir.name)
        materialized = adapter.materialize_device_adapter(WORKSPACE_ROOT, state_root, context=context, spec=normalized)
        source = state_root / materialized["source_relative_path"]
        source.write_text(source.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
        adapter_artifact = {
            "artifact_id": "art_444444444444444444444444",
            "requested_unit": "gpu",
            "result": normalized,
            "materialization": materialized,
        }
        with self.assertRaisesRegex(PlatformError, "哈希已变化"):
            adapter.run_generated_device_adapter(
                WORKSPACE_ROOT,
                state_root,
                adapter_artifact=adapter_artifact,
                probe_artifact={"artifact_id": self.probe["artifact_id"], "device": {"serial": "test-device-01"}},
            )


if __name__ == "__main__":
    unittest.main()
