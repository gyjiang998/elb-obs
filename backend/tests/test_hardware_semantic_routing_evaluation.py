from __future__ import annotations

import json
import unittest

from backend.hardware_semantic_routing_evaluation import (
    case_by_id,
    load_semantic_routing_suite,
    prepare_case_context,
    public_suite_catalog,
    score_case_result,
    summarize_case_scores,
)


def _profile() -> dict[str, object]:
    return {
        "schema_version": "target_platform_profile/v1",
        "profile_source": "user_selected",
        "capability_status": "not_device_probe",
        "chipset": {"family_id": "kirin", "family_label": "麒麟 SoC", "model_id": "kirin_9000", "model_label": "麒麟 9000"},
        "operating_system": {"family_id": "harmonyos", "family_label": "鸿蒙操作系统", "version_id": "harmonyos_5", "version_label": "HarmonyOS 5"},
    }


def _hardware(hardware_id: str, unit_type: str) -> dict[str, object]:
    return {
        "record_id": hardware_id,
        "record_type": "hardware_unit",
        "hardware_id": hardware_id,
        "unit_type": unit_type,
        "name": hardware_id,
        "software_interface": "source-derived interface",
        "supported_algorithms": [],
        "implementation_paths": [],
        "constraints": [],
    }


class HardwareSemanticRoutingEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suite = load_semantic_routing_suite()
        self.records = [
            _hardware("NNRT_HDI_DEVICE", "neural_network_device"),
            _hardware("GPU_VULKAN_RENDERING", "gpu"),
            _hardware("AUDIO_EFFECT_DSP_HDI", "audio_dsp"),
            _hardware("HCODEC_HDI", "video_codec"),
        ]

    def test_public_catalog_has_no_oracle_or_source_code(self) -> None:
        catalog = public_suite_catalog(self.suite)
        self.assertEqual(catalog["case_count"], 17)
        self.assertTrue(all("oracle" not in item and "public_input" not in item for item in catalog["cases"]))
        self.assertNotIn("ApplyStereoGain", json.dumps(catalog, ensure_ascii=False))

    def test_context_keeps_baseline_out_of_model_input(self) -> None:
        case = case_by_id(self.suite, "dsp_pcm_function_renamed")
        context = prepare_case_context(case, self.records, _profile())
        serialized = json.dumps(context["model_input_context"], ensure_ascii=False)
        self.assertNotIn("deterministic_router", serialized)
        self.assertNotIn("audio_data", serialized)
        self.assertIn("hardware_capability_cards", context["model_input_context"])

    def test_score_and_transformation_invariance_report(self) -> None:
        dsp_case = case_by_id(self.suite, "dsp_pcm_function_renamed")
        dsp_context = prepare_case_context(dsp_case, self.records, _profile())
        dsp_score = score_case_result(
            dsp_case,
            dsp_context,
            {
                "decision": "select_units",
                "summary": "可见样本数组的规则逐元素处理与 DSP 音频 effect 语义相容，仍需 provider 验证。",
                "semantic_profile": {"operation_pattern": ["规则逐元素浮点处理"]},
                "candidates": [{
                    "unit_id": "dsp",
                    "confidence": "medium",
                    "analysis_scope": "可见双层循环",
                    "observed_code_evidence": ["双层循环", "float 数组读写"],
                    "rationale": "逐样本变换可作为 DSP effect 的待验证候选。",
                    "evidence_ids": ["hardware:AUDIO_EFFECT_DSP_HDI"],
                }],
            },
        )
        negative_case = case_by_id(self.suite, "negative_semantic_redaction")
        negative_context = prepare_case_context(negative_case, self.records, _profile())
        negative_score = score_case_result(
            negative_case,
            negative_context,
            {
                "decision": "needs_compute_core",
                "summary": "通用数值循环缺失领域数据合同，不能可靠选择单元。",
                "semantic_profile": {"uncertainty": ["数据域未知"]},
                "candidates": [],
            },
        )
        self.assertTrue(dsp_score["correct"])
        self.assertTrue(negative_score["correct"])
        report = summarize_case_scores(self.suite, [dsp_score, negative_score])
        self.assertEqual(report["selection_recall"], 1.0)
        self.assertEqual(report["rejection_specificity"], 1.0)


if __name__ == "__main__":
    unittest.main()
