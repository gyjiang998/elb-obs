from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as platform_app
from backend.core import ArtifactStore, PlatformError
from backend.reasoning_lab import pasted_code_unit


class BlindReasoningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ArtifactStore(Path(self.tempdir.name), project_id="local-default")
        self.binding = {
            "declared_revision": None,
            "resolved_snapshot_id": "source_snapshot_test",
            "match_level": "unknown",
            "resolver_version": "1.0",
        }
        code = "void f(const float *a, float *b, int n) { for (int i = 0; i < n; ++i) b[i] = a[i] + 1.0f; }"
        self.code = self.store.write(
            "hotspot_code_unit",
            pasted_code_unit(code, "f", "pasted_code"),
            source_connector_id="local_test",
            source_binding=self.binding,
            producer={"name": "test", "version": "1.0"},
        )
        self.patch = patch.object(platform_app, "store", self.store)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.tempdir.cleanup()

    def test_blind_context_contains_only_minimal_model_input_and_isolated_session(self) -> None:
        response = platform_app.prepare_blind_reasoning(
            "local-default",
            platform_app.BlindReasoningPrepareRequest(
                analysis_id="analysis-main",
                code_artifact_id=self.code["artifact_id"],
                user_question="如何优化？",
            ),
        )

        context = response["artifact"]
        self.assertTrue(context["blind_session_analysis_id"].startswith("blind-"))
        self.assertNotEqual(context["blind_session_analysis_id"], "analysis-main")
        self.assertEqual(set(context["model_input"]), {"user_question", "target_platform_profile", "code_language", "code"})
        serialized = str(context["model_input"])
        self.assertNotIn("context_records", serialized)
        self.assertNotIn("hardware_route", serialized)
        self.assertNotIn("evidence_id", serialized)

    def test_blind_result_and_flow_comparison_are_persisted_with_claim_boundaries(self) -> None:
        prepared = platform_app.prepare_blind_reasoning(
            "local-default",
            platform_app.BlindReasoningPrepareRequest(
                analysis_id="analysis-main",
                code_artifact_id=self.code["artifact_id"],
                user_question="如何优化？",
            ),
        )["artifact"]
        blind = platform_app.save_blind_reasoning_result(
            "local-default",
            platform_app.BlindReasoningResultRequest(
                context_artifact_id=prepared["artifact_id"],
                result={
                    "summary": "规则逐元素循环可考虑 CPU SIMD 或 GPU。",
                    "confidence": "medium",
                    "workload_characterization": {"core_scope": "循环"},
                    "candidates": [{
                        "optimization_kind": "heterogeneous",
                        "hardware_unit": "gpu",
                        "target": "GPU compute",
                        "api_candidates": ["vkCmdDispatch"],
                        "rationale": "迭代独立。",
                    }],
                    "preferred_path": "先比较 SIMD 与 GPU 端到端开销。",
                    "unknowns": ["输入规模"],
                },
            ),
        )["artifact"]
        transcript = platform_app.save_reasoning_transcript(
            "local-default",
            platform_app.ReasoningTranscriptRequest(
                analysis_id="analysis-main",
                inference_id="inference-blind",
                run_id="run-blind",
                strategy_id="blind_baseline",
                strategy_label="独立盲推理",
                context_artifact_id=prepared["artifact_id"],
                result_artifact_id=blind["artifact_id"],
                system_prompt=str(prepared["prompt"]["system"]),
                user_prompt=str(prepared["prompt"]["user"]),
                raw_output='{"summary":"规则逐元素循环"}',
                outcome={"status": "succeeded"},
            ),
        )["artifact"]
        self.assertEqual(transcript["strategy_id"], "blind_baseline")
        self.assertEqual(transcript["context_pack"]["context_exclusions"][0], "optimization_kb")
        self.assertEqual(transcript["result_artifact_id"], blind["artifact_id"])
        flow_context = self.store.write(
            "reasoning_context_pack",
            {"strategy_id": "hardware_kb", "code": self.code["code"], "context_sha256": "test"},
            source_connector_id="local_test",
            source_binding=self.binding,
            producer={"name": "test", "version": "1.0"},
        )
        flow = self.store.write(
            "opportunity_reasoning_result",
            {"strategy_id": "hardware_kb", "context_artifact_id": flow_context["artifact_id"], "result": {"summary": "流程结果"}},
            source_connector_id="local_test",
            source_binding=self.binding,
            producer={"name": "test", "version": "1.0"},
        )
        device_run = self.store.write(
            "device_remote_shell_run",
            {
                "status": "succeeded",
                "execution": {"return_code": 0, "duration_ms": 12.5, "stdout": "FUNCTION_EQUIVALENT=PASS", "stderr": ""},
            },
            source_connector_id="local_test",
            source_binding=self.binding,
            producer={"name": "test", "version": "1.0"},
        )
        comparison_context = platform_app.prepare_blind_flow_comparison(
            "local-default",
            platform_app.BlindFlowComparisonPrepareRequest(
                blind_result_artifact_id=blind["artifact_id"],
                flow_result_artifact_id=flow["artifact_id"],
                device_run_artifact_id=device_run["artifact_id"],
            ),
        )["artifact"]
        self.assertEqual(comparison_context["device_run_artifact_id"], device_run["artifact_id"])
        self.assertEqual(comparison_context["model_input"]["device_validation_result"]["return_code"], 0)
        comparison = platform_app.save_blind_flow_comparison_result(
            "local-default",
            platform_app.BlindFlowComparisonResultRequest(
                context_artifact_id=comparison_context["artifact_id"],
                result={
                    "verdict": "complementary",
                    "summary": "盲推理召回方向，流程补充证据边界。",
                    "agreements": ["GPU 是待验证方向"],
                    "disagreements": [],
                    "blind_strengths": ["简洁"],
                    "blind_weaknesses": ["无设备证据"],
                    "flow_strengths": ["有 API 上下文"],
                    "flow_weaknesses": ["门禁偏保守"],
                    "unsupported_claims": [],
                    "merged_recommendation": "先做 CPU SIMD，再按大输入验证 GPU。",
                },
            ),
        )["artifact"]

        self.assertEqual(comparison["result"]["verdict"], "complementary")
        self.assertIn("不是真值", comparison["result"]["evaluation_boundary_zh"])
        self.assertEqual(comparison["blind_session_analysis_id"], blind["blind_session_analysis_id"])

    def test_comparison_rejects_a_different_hotspot(self) -> None:
        prepared = platform_app.prepare_blind_reasoning(
            "local-default",
            platform_app.BlindReasoningPrepareRequest(analysis_id="analysis-main", code_artifact_id=self.code["artifact_id"]),
        )["artifact"]
        blind = platform_app.save_blind_reasoning_result(
            "local-default",
            platform_app.BlindReasoningResultRequest(
                context_artifact_id=prepared["artifact_id"],
                result={"summary": "摘要", "preferred_path": "路径", "candidates": [], "unknowns": []},
            ),
        )["artifact"]
        other_context = self.store.write(
            "reasoning_context_pack",
            {"strategy_id": "hardware_kb", "code": "void other() {}"},
            source_connector_id="local_test",
            source_binding=self.binding,
            producer={"name": "test", "version": "1.0"},
        )
        flow = self.store.write(
            "opportunity_reasoning_result",
            {"strategy_id": "hardware_kb", "context_artifact_id": other_context["artifact_id"], "result": {}},
            source_connector_id="local_test",
            source_binding=self.binding,
            producer={"name": "test", "version": "1.0"},
        )

        with self.assertRaises(PlatformError) as raised:
            platform_app.prepare_blind_flow_comparison(
                "local-default",
                platform_app.BlindFlowComparisonPrepareRequest(
                    blind_result_artifact_id=blind["artifact_id"],
                    flow_result_artifact_id=flow["artifact_id"],
                ),
            )
        self.assertEqual(raised.exception.code, "BLIND_COMPARISON_INPUT_MISMATCH")


if __name__ == "__main__":
    unittest.main()
