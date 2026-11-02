from __future__ import annotations

import unittest

from backend.device_dispatch import build_device_dispatch_context, validate_device_dispatch_assessment


class DeviceDispatchContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.code = {
            "artifact_id": "art_code_12345678901234567890",
            "code": """
void RenamedComputation(float* output, const float* input, int count)
{
    for (int index = 0; index < count; ++index) {
        output[index] = input[index] * 1.2F + 0.1F;
    }
}
""".strip(),
            "source": {"origin": "user_paste", "binding_level": "unbound", "line_start": 1, "line_end": 6},
        }
        self.reasoning_result = {
            "artifact_id": "art_result_123456789012345678",
            "strategy_id": "hardware_kb",
            "result": {
                "recommendation": "evidence_required",
                "confidence": "low",
                "evidence_status": "model_hypothesis",
                "summary": "可见数组变换可继续检查 GPU 图像/缓冲路径。",
                "opportunities": [{
                    "opportunity_kind": "heterogeneous",
                    "candidate_target": "gpu",
                    "analysis_scope": {"level": "loop_region", "status": "observed", "description": "第 3-5 行逐元素变换", "source_evidence": "可见循环和 float 指针读写"},
                    "rationale": "逐元素数组变换。",
                    "required_conditions": [],
                    "risks": [],
                    "verification_plan": [],
                    "evidence_ids": ["hardware:GPU_SHADER"],
                }],
            },
        }
        self.reasoning_context = {
            "artifact_id": "art_context_12345678901234567",
            "model_input_context": {
                "code": self.code["code"],
                "code_language": "cpp",
                "code_observations": {"analysis_method": "lexical_static_scan/v1", "loop_construct_count": 1},
                "candidate_scope_method": "lexical_scope_selection/v1",
                "candidate_scopes": [{"level": "loop_region", "line_start": 3, "line_end": 5, "description_zh": "第 3-5 行逐元素变换", "resolution_status": "observed"}],
                "hardware_routing": {"selection_origin": "model", "selected_units": [{"unit_id": "gpu"}]},
                "context_records": [{
                    "evidence_id": "hardware:GPU_SHADER",
                    "record": {"unit_type": "gpu", "name": "GPU Shader", "one_line_abstract_zh": "适合规则像素或缓冲元素变换"},
                }, {
                    "evidence_id": "accelerator_api:gpu:OH_GLES3",
                    "record": {"accelerator_unit": "gpu", "symbol": "OpenGL ES", "one_line_abstract_zh": "图形/计算着色路径候选"},
                }],
                "allowed_evidence_ids": ["hardware:GPU_SHADER", "accelerator_api:gpu:OH_GLES3", "trace:pasted_code", "source:abc"],
            },
        }
        self.probe = {
            "artifact_id": "art_probe_1234567890123456789",
            "observed_units": {"gpu": {"observation_status": "observed", "observed_capabilities": ["GLES3"], "claim_boundary_zh": "仍需验证格式。"}},
        }

    def _context(self) -> dict[str, object]:
        return build_device_dispatch_context(
            code_artifact=self.code,
            reasoning_result_artifact=self.reasoning_result,
            reasoning_context_artifact=self.reasoning_context,
            probe_artifact=self.probe,
            requested_unit="gpu",
        )

    def _semantic_candidate(self) -> dict[str, object]:
        return {
            "schema_version": "device_dispatch_assessment/v1",
            "decision": "semantic_candidate",
            "target_unit": "gpu",
            "execution_mode": "gles_buffer_transform",
            "summary": "可见逐元素浮点变换可形成 GPU 下发候选，但格式和搬运仍待验证。",
            "reasoning": "代码中存在一段只读 input、只写 output 的逐元素循环；GPU 能力卡和 API 记录只提供候选方向，设备仍需验证。",
            "candidate_scope": {"level": "loop_region", "status": "observed", "description": "第 3-5 行逐元素变换", "source_evidence": "for 循环与 float 指针读写"},
            "observed_code_evidence": ["循环内每次写入 output[index]，读取 input[index]，可见迭代间未出现共享累加。"],
            "dispatch_contract": {
                "semantic_operations": ["逐元素乘加"],
                "input_assumptions": ["连续 float 输入输出布局待 adapter 确认"],
                "output_semantics": ["输出与 CPU 浮点计算保持允许容差内一致"],
                "state_and_side_effects": ["当前可见循环没有外部副作用；别名关系待确认"],
                "required_adapter_work": ["建立数据上传、shader 或计算管线以及同步合同"],
                "cpu_fallback": ["保留现有 CPU 循环作为 fallback"],
                "result_comparison": ["用固定输入比较 CPU 与 GPU 输出的绝对/相对误差"],
            },
            "required_device_checks": ["验证目标格式、context、同步和端到端搬运成本"],
            "risks": ["小批量时数据搬运成本可能超过计算收益"],
            "alternative_units": [],
            "evidence_ids": ["hardware:GPU_SHADER", "accelerator_api:gpu:OH_GLES3"],
        }

    def test_context_uses_semantic_evidence_without_a_reference_source_match(self) -> None:
        context = self._context()
        model_input = context["model_input_context"]

        self.assertEqual(context["requested_unit"], "gpu")
        self.assertIn("RenamedComputation", model_input["code"])
        self.assertEqual(model_input["hardware_routing"]["selected_units"], ["gpu"])
        self.assertEqual(len(model_input["selected_unit_evidence"]), 2)
        self.assertNotIn("reference_source", model_input)

    def test_complete_model_contract_is_valid_after_cosmetic_function_renaming(self) -> None:
        context = self._context()
        result, errors = validate_device_dispatch_assessment(self._semantic_candidate(), context)

        self.assertEqual(errors, [])
        self.assertEqual(result["validation"]["status"], "valid")
        self.assertEqual(result["decision"], "semantic_candidate")
        self.assertEqual(result["dispatch_contract"]["semantic_operations"], ["逐元素乘加"])

    def test_candidate_without_an_auditable_contract_is_downgraded_not_source_rejected(self) -> None:
        context = self._context()
        candidate = self._semantic_candidate()
        candidate["dispatch_contract"] = {}
        result, errors = validate_device_dispatch_assessment(candidate, context)

        self.assertTrue(errors)
        self.assertEqual(result["decision"], "needs_contract")
        self.assertIn("不是由源码文本匹配导致", result["validation"]["calibration_actions"][0])


if __name__ == "__main__":
    unittest.main()
