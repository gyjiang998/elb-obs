from __future__ import annotations

import unittest

from backend.model_semantic_api_retrieval import rank_api_candidates_from_model_route
from backend.reasoning_lab import pasted_code_unit, prepare_context


def _route(unit_id: str, profile: dict[str, list[str]]) -> dict[str, object]:
    return {
        "decision": "select_units",
        "semantic_profile": profile,
        "candidates": [{
            "unit_id": unit_id,
            "confidence": "medium",
            "analysis_scope": "可见循环",
            "observed_code_evidence": ["规则数组读写"],
            "rationale": "待验证的语义方向。",
            "evidence_ids": ["hardware:AUDIO_EFFECT_DSP_HDI"],
        }],
        "validation": {"status": "valid"},
    }


class ModelSemanticApiRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsp_record = {
            "record_id": "dsp_api:AudioEffectProcess",
            "record_type": "accelerator_api_entrypoint",
            "accelerator_unit": "dsp",
            "symbol": "AudioEffectProcess",
            "name": "DSP PCM effect",
            "one_line_abstract_zh": "对逐帧 PCM 样本执行音效处理。",
            "semantic_family": {"id": "dsp_pcm_effect", "label_zh": "PCM 音效处理"},
            "code_feature_match": {"expected_data_forms": ["PCM frame samples"]},
        }

    def test_semantic_profile_not_source_keywords_selects_matching_api_card(self) -> None:
        ranked, summary = rank_api_candidates_from_model_route(
            _route("dsp", {"data_representation": ["PCM 样本帧"], "operation_pattern": ["逐帧音效处理"]}),
            npu_records=[],
            accelerator_records={"dsp": [self.dsp_record]},
            per_unit_limit=2,
        )
        self.assertEqual(summary["method"], "model_semantic_profile_text_retrieval/v1")
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["evidence_id"], "accelerator_api:dsp_api:AudioEffectProcess")
        self.assertEqual(ranked[0]["retrieval_rationale"]["selected_unit"], "dsp")

    def test_no_profile_overlap_does_not_fall_back_to_unit_specific_rule(self) -> None:
        ranked, summary = rank_api_candidates_from_model_route(
            _route("dsp", {"data_representation": ["opaque scalar values"], "operation_pattern": ["unknown state transition"]}),
            npu_records=[],
            accelerator_records={"dsp": [self.dsp_record]},
            per_unit_limit=2,
        )
        self.assertEqual(ranked, [])
        self.assertEqual(summary["unit_results"]["dsp"]["status"], "no_semantic_profile_text_overlap")

    def test_user_built_catalog_is_searched_only_after_its_exact_route_is_selected(self) -> None:
        kb_id = "custom_api_kb_" + "b" * 24
        unit_id = f"custom:{kb_id}"
        custom_api = {
            "record_id": "custom_api:aae_process_pcm",
            "record_type": "custom_accelerator_api_candidate",
            "accelerator_unit": "AAE",
            "symbol": "AAE_ProcessPcm",
            "one_line_abstract_zh": "对逐帧 PCM 样本执行音效处理。",
            "semantic_family": {"id": "custom_execution", "label_zh": "PCM 音效执行"},
            "code_feature_match": {"expected_data_forms": ["PCM frame samples"]},
        }
        route = {
            "decision": "select_units",
            "semantic_profile": {
                "data_representation": ["PCM 样本帧"],
                "operation_pattern": ["逐帧音效处理"],
            },
            "candidates": [{
                "unit_id": unit_id,
                "confidence": "medium",
                "analysis_scope": "PCM 处理循环",
                "observed_code_evidence": ["PCM 样本数组"],
                "rationale": "与自建 AAE 目录语义相容。",
                "evidence_ids": [f"custom_api_kb:{kb_id}"],
            }],
            "validation": {"status": "valid"},
        }

        ranked, summary = rank_api_candidates_from_model_route(
            route,
            npu_records=[],
            accelerator_records={"dsp": [self.dsp_record]},
            custom_records={unit_id: [custom_api]},
            per_unit_limit=2,
        )

        self.assertEqual(summary["primary_unit_id"], unit_id)
        self.assertEqual(summary["selected_units"], [unit_id])
        self.assertEqual(ranked[0]["evidence_id"], "custom_api_record:custom_api:aae_process_pcm")
        self.assertEqual(ranked[0]["retrieval_rationale"]["selected_unit"], unit_id)

    def test_user_built_route_and_api_evidence_reach_opportunity_model_context(self) -> None:
        kb_id = "custom_api_kb_" + "c" * 24
        unit_id = f"custom:{kb_id}"
        route_evidence_id = f"custom_api_kb:{kb_id}"
        api_record = {
            "record_id": "custom_api:aae_process_pcm",
            "record_type": "custom_accelerator_api_candidate",
            "accelerator_unit": "AAE",
            "symbol": "AAE_ProcessPcm",
            "one_line_abstract_zh": "对逐帧 PCM 样本执行音效处理。",
            "semantic_family": {"id": "custom_execution", "label_zh": "PCM 音效执行"},
            "code_feature_match": {"expected_data_forms": ["PCM frame samples"]},
            "runtime_verification": ["核对样本格式、帧大小、provider 和 CPU fallback。"],
        }
        routing_record = {
            "record_id": kb_id,
            "record_type": "custom_hardware_api_directory",
            "hardware_id": kb_id,
            "routing_unit_id": unit_id,
            "routing_evidence_id": route_evidence_id,
            "custom_api_kb_id": kb_id,
            "name": "AAE API（自建）",
            "constraints": ["静态候选，仍需设备验证"],
        }
        route = {
            "decision": "select_units",
            "selection_origin": "model",
            "semantic_profile": {
                "data_representation": ["PCM 样本帧"],
                "operation_pattern": ["逐帧音效处理"],
            },
            "candidates": [{
                "unit_id": unit_id,
                "confidence": "medium",
                "analysis_scope": "PCM 处理循环",
                "observed_code_evidence": ["PCM 样本数组"],
                "rationale": "与自建 AAE 目录语义相容。",
                "evidence_ids": [route_evidence_id],
            }],
            "validation": {"status": "valid"},
        }
        context = prepare_context(
            "hardware_kb",
            pasted_code_unit("void F(float *pcm, size_t n) { for (size_t i = 0; i < n; ++i) pcm[i] *= 0.5F; }", "F", "F"),
            [],
            [],
            [routing_record],
            optimization_kb_metadata={"kb_id": "o", "version": "v", "content_sha256": "o" * 64},
            hardware_kb_metadata={"kb_id": "h", "version": "v", "content_sha256": "h" * 64},
            custom_api_records={unit_id: [api_record]},
            custom_api_kb_metadata={unit_id: {"kb_id": kb_id, "content_sha256": "c" * 64}},
            hardware_semantic_route=route,
        )

        self.assertEqual(context["hardware_routing"]["primary_unit_id"], unit_id)
        self.assertEqual(context["retrieval"]["custom_api"]["searched_units"], [unit_id])
        evidence_ids = [item["evidence_id"] for item in context["model_input_context"]["context_records"]]
        self.assertEqual(evidence_ids, [route_evidence_id, "custom_api_record:custom_api:aae_process_pcm"])
        self.assertEqual(context["model_input_context"]["hardware_routing"]["selected_units"], [{"unit_id": unit_id}])

    def test_route_rationale_cannot_preselect_an_api_for_the_next_stage(self) -> None:
        route = _route("dsp", {"data_representation": ["opaque scalar values"], "operation_pattern": ["unknown state transition"]})
        route["candidates"][0]["rationale"] = "直接选择 AudioEffectProcess。"

        ranked, _ = rank_api_candidates_from_model_route(
            route,
            npu_records=[],
            accelerator_records={"dsp": [self.dsp_record]},
            per_unit_limit=2,
        )

        self.assertEqual(ranked, [])

    def test_hardware_context_uses_model_route_without_invoking_legacy_router(self) -> None:
        hardware_record = {
            "record_id": "AUDIO_EFFECT_DSP_HDI",
            "record_type": "hardware_unit",
            "hardware_id": "AUDIO_EFFECT_DSP_HDI",
            "unit_type": "audio_dsp",
            "name": "Audio Effect DSP HDI",
            "software_interface": "Audio Effect HDI",
            "supported_algorithms": ["PCM effect"],
            "implementation_paths": [],
            "constraints": [],
        }
        context = prepare_context(
            "hardware_kb",
            pasted_code_unit("void F(float *v, size_t n) { for (size_t i = 0; i < n; ++i) v[i] *= 0.5F; }", "F", "F"),
            [],
            [],
            [hardware_record],
            optimization_kb_metadata={"kb_id": "o", "version": "v", "content_sha256": "o" * 64},
            hardware_kb_metadata={"kb_id": "h", "version": "v", "content_sha256": "h" * 64},
            accelerator_api_records={"dsp": [self.dsp_record]},
            hardware_semantic_route=_route("dsp", {"data_representation": ["PCM 样本帧"], "operation_pattern": ["逐帧音效处理"]}),
        )
        self.assertEqual(context["hardware_routing"]["method"], "model_semantic_routing/v2")
        self.assertEqual(context["retrieval"]["method"], "model_semantic_route_api_funnel/v2")
        model_input = context["model_input_context"]
        self.assertEqual(model_input["hardware_routing"], {
            "selection_origin": "model",
            "primary_unit_id": "dsp",
            "selected_units": [{"unit_id": "dsp"}],
        })
        self.assertNotIn("semantic_profile", model_input["hardware_routing"])
        self.assertNotIn("rationale", str(model_input["hardware_routing"]))
        self.assertTrue(all("retrieval_rationale" not in item for item in model_input["context_records"]))
        self.assertTrue(all("retrieval_match_terms" not in item for item in model_input["context_records"]))
        evidence_ids = [item["evidence_id"] for item in model_input["context_records"]]
        self.assertIn("accelerator_api:dsp_api:AudioEffectProcess", evidence_ids)

    def test_hardware_context_keeps_the_complete_api_top_n_with_rich_chinese_cards(self) -> None:
        hardware_record = {
            "record_id": "AUDIO_EFFECT_DSP_HDI",
            "record_type": "hardware_unit",
            "hardware_id": "AUDIO_EFFECT_DSP_HDI",
            "unit_type": "audio_dsp",
            "name": "Audio Effect DSP HDI",
            "constraints": [],
        }
        verification = [
            f"验证真实 PCM 帧的格式、所有权、时延、同步和软件回退语义 {index}。" * 5
            for index in range(4)
        ]
        api_records = [
            {
                "record_id": f"dsp_api:Process{index}",
                "record_type": "accelerator_api_entrypoint",
                "accelerator_unit": "dsp",
                "symbol": f"Process{index}",
                "name": f"DSP PCM effect {index}",
                "one_line_abstract_zh": "对逐帧 PCM 样本执行音效处理。",
                "semantic_family": {"id": "dsp_pcm_effect", "label_zh": "PCM 音效处理"},
                "code_feature_match": {
                    "expected_data_forms": ["PCM 样本帧"],
                    "ast_signals": ["连续音频数组", "逐帧独立处理"],
                },
                "runtime_verification": verification,
            }
            for index in range(4)
        ]
        context = prepare_context(
            "hardware_kb",
            pasted_code_unit("void F(float *v, size_t n) { for (size_t i = 0; i < n; ++i) v[i] *= 0.5F; }", "F", "F"),
            [],
            [],
            [hardware_record],
            optimization_kb_metadata={"kb_id": "o", "version": "v", "content_sha256": "o" * 64},
            hardware_kb_metadata={"kb_id": "h", "version": "v", "content_sha256": "h" * 64},
            accelerator_api_records={"dsp": api_records},
            hardware_semantic_route=_route("dsp", {"data_representation": ["PCM 样本帧"], "operation_pattern": ["逐帧音效处理"]}),
        )

        evidence_ids = [item["evidence_id"] for item in context["model_input_context"]["context_records"]]
        self.assertEqual(len(evidence_ids), 5)
        self.assertEqual(evidence_ids[0], "hardware:AUDIO_EFFECT_DSP_HDI")
        self.assertEqual(set(evidence_ids[1:]), {f"accelerator_api:dsp_api:Process{index}" for index in range(4)})
        self.assertFalse(context["retrieval"]["truncated"])

    def test_primary_gpu_route_never_retrieves_higher_scoring_npu_api(self) -> None:
        gpu_record = {
            "record_id": "gpu_api:VulkanComputeDispatch",
            "record_type": "accelerator_api_entrypoint",
            "accelerator_unit": "gpu",
            "symbol": "vkCmdDispatch",
            "name": "Vulkan compute dispatch",
            "one_line_abstract_zh": "对规则 RGBA buffer 执行并行 compute shader。",
            "semantic_family": {"id": "gpu_compute", "label_zh": "GPU 通用计算"},
        }
        npu_record = {
            "record_id": "nnrt_op:OH_NN_OPS_MATMUL",
            "record_type": "npu_operator",
            "symbol": "OH_NN_OPS_MATMUL",
            "name": "MatMul",
            "one_line_abstract_zh": "RGBA buffer matrix compute parallel clamp brightness tensor",
        }
        route = {
            "decision": "select_units",
            "selection_origin": "model",
            "semantic_profile": {
                "data_representation": ["RGBA buffer"],
                "operation_pattern": ["parallel compute brightness clamp matrix tensor"],
            },
            "candidates": [
                {"unit_id": "gpu", "confidence": "high", "rationale": "规则像素并行。"},
                {"unit_id": "npu", "confidence": "low", "rationale": "仅作备选。"},
            ],
            "validation": {"status": "valid"},
        }

        ranked, summary = rank_api_candidates_from_model_route(
            route,
            npu_records=[npu_record],
            accelerator_records={"gpu": [gpu_record]},
            per_unit_limit=4,
        )

        self.assertEqual(summary["primary_unit_id"], "gpu")
        self.assertEqual(summary["selected_units"], ["gpu"])
        self.assertEqual(summary["alternative_units_not_searched"], ["npu"])
        self.assertTrue(ranked)
        self.assertTrue(all(item["retrieval_rationale"]["selected_unit"] == "gpu" for item in ranked))
        self.assertFalse(any(item["evidence_id"].startswith("npu_api:") for item in ranked))


if __name__ == "__main__":
    unittest.main()
