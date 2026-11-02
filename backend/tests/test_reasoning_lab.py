from __future__ import annotations

import unittest

from backend.reasoning_lab import adapt_reasoning_result, compare_results, pasted_code_unit, prepare_context, validate_reasoning_result


class ReasoningLabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.code_unit = pasted_code_unit(
            "void Brightness(uint8_t *pixels, size_t count) { for (size_t i = 0; i < count; ++i) pixels[i] += 1; }",
            "GpuBrightness",
            "GpuBrightness",
        )
        self.formal_records = [
            {
                "record_id": "sig_pixel_lut",
                "name": "Pixel LUT traversal",
                "algorithm": "brightness",
                "target_unit": "GPU_SHADER",
            }
        ]
        self.cases = [
            {
                "case_id": "case_brightness_gpu",
                "name": "GPU brightness path",
                "algorithm": "brightness",
                "target_unit": "GPU_SHADER",
            }
        ]
        self.hardware_records = [
            {
                "hardware_id": "GPU_SHADER",
                "unit_type": "gpu",
                "constraints": {"data_types": ["uint8"]},
            }
        ]

    def _context(self, strategy_id: str) -> dict[str, object]:
        return prepare_context(
            strategy_id,
            self.code_unit,
            self.formal_records,
            self.cases,
            self.hardware_records,
            optimization_kb_metadata={"kb_id": "optimization-test", "version": "test-v1", "content_sha256": "o" * 64},
            hardware_kb_metadata={"kb_id": "hardware-test", "version": "test-v1", "content_sha256": "h" * 64},
            allow_legacy_static_baseline=strategy_id == "hardware_kb",
        )

    def test_direct_baseline_cannot_receive_kb_or_hardware_records(self) -> None:
        context = self._context("direct_baseline")
        self.assertEqual(context["retrieval"]["method"], "none")
        self.assertEqual(context["context_records"], [])

    def test_target_platform_profile_is_audited_without_becoming_capability_evidence(self) -> None:
        profile = {
            "schema_version": "target_platform_profile/v1",
            "profile_source": "user_selected",
            "capability_status": "not_device_probe",
            "chipset": {
                "family_id": "kirin",
                "family_label": "麒麟 SoC",
                "model_id": "kirin_9000s",
                "model_label": "麒麟 9000S",
            },
            "operating_system": {
                "family_id": "harmonyos",
                "family_label": "鸿蒙操作系统",
                "version_id": "harmonyos_5",
                "version_label": "HarmonyOS 5.x / HarmonyOS NEXT",
            },
        }
        context = prepare_context(
            "direct_baseline",
            self.code_unit,
            self.formal_records,
            self.cases,
            self.hardware_records,
            optimization_kb_metadata={"kb_id": "optimization-test", "version": "test-v1", "content_sha256": "o" * 64},
            hardware_kb_metadata={"kb_id": "hardware-test", "version": "test-v1", "content_sha256": "h" * 64},
            target_platform_profile=profile,
        )

        self.assertEqual(context["target_platform_profile"], profile)
        self.assertEqual(context["context_records"], [])
        self.assertEqual(context["target_platform_profile"]["capability_status"], "not_device_probe")
        self.assertIn("not a device probe", " ".join(context["common_constraints"]))

    def test_context_packs_keep_evidence_namespaces_isolated(self) -> None:
        kb_context = self._context("kb_rag")
        hardware_context = self._context("hardware_kb")
        self.assertTrue(kb_context["context_records"])
        self.assertTrue(hardware_context["context_records"])
        self.assertTrue(all(item["evidence_id"].startswith("kb:") for item in kb_context["context_records"]))
        self.assertTrue(all(item["evidence_id"].startswith("hardware:") for item in hardware_context["context_records"]))

    def test_hardware_context_rejects_an_implicit_static_router(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a validated model semantic route"):
            prepare_context(
                "hardware_kb",
                self.code_unit,
                self.formal_records,
                self.cases,
                self.hardware_records,
                optimization_kb_metadata={"kb_id": "optimization-test", "version": "test-v1", "content_sha256": "o" * 64},
                hardware_kb_metadata={"kb_id": "hardware-test", "version": "test-v1", "content_sha256": "h" * 64},
            )

    def test_model_input_projection_keeps_semantics_and_excludes_audit_noise(self) -> None:
        self.code_unit["hotspot"]["trace_metrics"] = {
            "first_start_ns": 1_000_000,
            "last_end_ns": 2_000_000,
            "self_time_estimate_total_duration_ms": 0.25,
            "max_self_time_estimate_duration_ms": 0.1,
            "inclusive_total_duration_ms": 0.5,
            "max_span_duration_ms": 0.2,
            "occurrence_count": 5,
        }
        context = self._context("kb_rag")
        model_context = context["model_input_context"]

        self.assertEqual(model_context["schema_version"], "model_input_context/v8")
        self.assertEqual(model_context["code"], self.code_unit["code"])
        self.assertEqual(model_context["hotspot"]["trace_metrics"]["observation_window_ms"], 1.0)
        self.assertEqual(model_context["hotspot"]["trace_metrics"]["self_time_share_of_window_percent"], 25.0)
        self.assertEqual(model_context["hotspot"]["trace_metrics"]["occurrences_per_second"], 5000.0)
        self.assertEqual(model_context["hotspot"]["trace_metrics"]["average_self_time_estimate_duration_ms"], 0.05)
        self.assertEqual(model_context["hotspot"]["trace_metrics"]["average_inclusive_duration_ms"], 0.1)
        self.assertEqual(model_context["hotspot"]["trace_metrics"]["self_to_inclusive_duration_percent"], 50.0)
        self.assertEqual(model_context["code_observations"]["analysis_method"], "lexical_static_scan/v1")
        self.assertEqual(model_context["candidate_scope_method"], "lexical_scope_selection/v1")
        self.assertEqual(model_context["code_observations"]["loop_construct_count"], 1)
        self.assertEqual(model_context["code_observations"]["ipc_transaction_symbols"], [])
        self.assertTrue(model_context["candidate_scopes"])
        self.assertEqual(model_context["candidate_scopes"][0]["level"], "loop_region")
        self.assertNotIn("event_id", model_context["hotspot"])
        self.assertNotIn("sha256", model_context["source"])
        self.assertNotIn("terms", model_context["retrieval"])
        self.assertNotIn("optimization_kb", model_context)
        self.assertNotIn("hardware_kb", model_context)
        self.assertLessEqual(len(model_context["context_records"]), 4)
        self.assertIn("trace:pasted_code", model_context["allowed_evidence_ids"])
        self.assertIn(f"source:{self.code_unit['source']['sha256']}", model_context["allowed_evidence_ids"])
        self.assertTrue(any("不是设备探测" in item for item in model_context["constraints"]))
        self.assertTrue(any("output_contract" in item for item in model_context["constraints"]))

    def test_model_context_keeps_visible_core_and_marks_unexpanded_callee(self) -> None:
        wrapped_code = """
void ComputeWrapper(float *output, const float *input, size_t count)
{
    APP_LOGI(\"begin\");
    if (output == nullptr || input == nullptr) {
        return;
    }
    for (size_t row = 0; row < 2; ++row) {
        for (size_t index = 0; index < count; ++index) {
            output[index] = input[index] * 1.25F + 0.5F;
        }
    }
    astcenc_compress_image(output, count);
}
""".strip()
        code_unit = pasted_code_unit(wrapped_code, "ComputeWrapper", "ComputeWrapper")
        context = prepare_context(
            "direct_baseline",
            code_unit,
            [],
            [],
            [],
            optimization_kb_metadata={"kb_id": "optimization-test", "version": "test-v1", "content_sha256": "o" * 64},
            hardware_kb_metadata={"kb_id": "hardware-test", "version": "test-v1", "content_sha256": "h" * 64},
        )
        scopes = context["model_input_context"]["candidate_scopes"]

        loop_scope = next(scope for scope in scopes if scope["level"] == "loop_region")
        callee_scope = next(scope for scope in scopes if scope.get("symbol") == "astcenc_compress_image")
        self.assertEqual(loop_scope["resolution_status"], "observed")
        self.assertIn("循环", loop_scope["signals"])
        self.assertEqual(sum(scope["level"] == "loop_region" for scope in scopes), 1)
        self.assertEqual(callee_scope["level"], "callee")
        self.assertEqual(callee_scope["resolution_status"], "callee_unresolved")
        self.assertFalse(any(scope.get("symbol") == "ComputeWrapper" for scope in scopes))

    def test_hardware_context_keeps_accelerator_api_candidate_and_its_boundary(self) -> None:
        gpu_record = {
            "record_id": "gpu_api:GpuBrightnessAlgo::OnApplyRGBA8888",
            "record_type": "accelerator_api_entrypoint",
            "accelerator_unit": "gpu",
            "symbol": "GpuBrightnessAlgo::OnApplyRGBA8888",
            "name": "GPU brightness path",
            "semantic_family": {"id": "gpu_image_effect", "label_zh": "GPU 图像效果路径"},
            "code_feature_match": {"operator_aliases": ["brightness", "rgba8888"]},
            "capability_boundary": {"target_device_availability": "evidence_required"},
            "source_contract": {"source_file": "brightness.cpp", "exposure": "system_source_integration"},
        }
        context = prepare_context(
            "hardware_kb",
            self.code_unit,
            [],
            [],
            self.hardware_records,
            optimization_kb_metadata={"kb_id": "optimization-test", "version": "test-v1", "content_sha256": "o" * 64},
            hardware_kb_metadata={"kb_id": "hardware-test", "version": "test-v1", "content_sha256": "h" * 64},
            accelerator_api_records={"gpu": [gpu_record]},
            accelerator_api_kb_metadata={"gpu": {"kb_id": "gpu-test", "version": "test-v1", "content_sha256": "g" * 64}},
            accelerator_api_manifest_metadata={"manifest_id": "accelerator-test", "version": "test-v1", "content_sha256": "a" * 64},
            allow_legacy_static_baseline=True,
        )

        self.assertEqual(context["retrieval"]["method"], "legacy_static_hardware_unit_api_funnel/v1")
        self.assertEqual(context["retrieval"]["selected_units"], ["gpu"])
        self.assertEqual(context["hardware_routing"]["selected_units"][0]["unit_id"], "gpu")
        self.assertEqual(context["context_records"][0]["evidence_id"], "hardware:GPU_SHADER")
        api_record = next(item for item in context["context_records"] if item["evidence_id"].startswith("accelerator_api:"))
        self.assertEqual(api_record["evidence_id"], "accelerator_api:gpu_api:GpuBrightnessAlgo::OnApplyRGBA8888")
        self.assertEqual(context["accelerator_api_kbs"]["gpu"]["kb_id"], "gpu-test")
        self.assertEqual(context["accelerator_api_manifest"]["manifest_id"], "accelerator-test")
        projected = next(
            item["record"]
            for item in context["model_input_context"]["context_records"]
            if item["evidence_id"].startswith("accelerator_api:")
        )
        self.assertEqual(projected["accelerator_unit"], "gpu")
        self.assertEqual(projected["source_contract"]["exposure"], "system_source_integration")
        self.assertEqual(context["model_input_context"]["hardware_routing"]["selected_units"][0]["unit_id"], "gpu")

    def test_hardware_funnel_never_mixes_an_unselected_npu_api_into_gpu_context(self) -> None:
        gpu_record = {
            "record_id": "gpu_api:GpuBrightnessAlgo::OnApplyRGBA8888",
            "record_type": "accelerator_api_entrypoint",
            "accelerator_unit": "gpu",
            "symbol": "GpuBrightnessAlgo::OnApplyRGBA8888",
            "semantic_family": {"id": "gpu_image_effect"},
            "code_feature_match": {"operator_aliases": ["brightness", "pixels"]},
        }
        npu_record = {
            "record_id": "nnrt_op:OH_NN_OPS_ADD",
            "record_type": "npu_operation",
            "symbol": "OH_NN_OPS_ADD",
            "semantic_family": {"id": "elementwise_arithmetic"},
            "code_feature_match": {"operator_aliases": ["add"]},
        }
        context = prepare_context(
            "hardware_kb",
            self.code_unit,
            [],
            [],
            self.hardware_records,
            optimization_kb_metadata={"kb_id": "optimization-test", "version": "test-v1", "content_sha256": "o" * 64},
            hardware_kb_metadata={"kb_id": "hardware-test", "version": "test-v1", "content_sha256": "h" * 64},
            npu_api_records=[npu_record],
            accelerator_api_records={"gpu": [gpu_record]},
            allow_legacy_static_baseline=True,
        )

        evidence_ids = [item["evidence_id"] for item in context["model_input_context"]["context_records"]]
        self.assertIn("accelerator_api:gpu_api:GpuBrightnessAlgo::OnApplyRGBA8888", evidence_ids)
        self.assertFalse(any(item.startswith("npu_api:") for item in evidence_ids))
        self.assertEqual(context["retrieval"]["npu_api"]["status"], "not_selected_by_hardware_router")
        self.assertEqual(context["retrieval"]["accelerator_api"]["searched_units"], ["gpu"])

    def test_direct_baseline_model_projection_has_no_retrieval_record(self) -> None:
        context = self._context("direct_baseline")
        model_context = context["model_input_context"]
        self.assertEqual(model_context["retrieval"]["method"], "none")
        self.assertEqual(model_context["retrieval"]["matched_evidence_count"], 0)
        self.assertEqual(model_context["context_records"], [])

    def test_generic_cpp_terms_do_not_retrieve_an_unrelated_accelerator_case(self) -> None:
        generic_code = pasted_code_unit(
            "bool BundleProxy::GetNameForUid(const int uid) { APP_LOGE(\"fail\"); return uid > Constants::BASE_APP_UID; }",
            "BundleProxy::GetNameForUid",
            "BundleProxy::GetNameForUid",
        )
        unrelated_case = {
            "record_id": "astc_opencl_fallback",
            "name": "ASTC OpenCL encode with CPU software fallback",
            "algorithm": "astc_texture_encode",
            "target_unit": "GPU_OPENCL",
        }
        context = prepare_context(
            "kb_rag",
            generic_code,
            [unrelated_case],
            [],
            [],
            optimization_kb_metadata={"kb_id": "optimization-test", "version": "test-v1", "content_sha256": "o" * 64},
            hardware_kb_metadata={"kb_id": "hardware-test", "version": "test-v1", "content_sha256": "h" * 64},
        )
        self.assertEqual(context["context_records"], [])
        self.assertEqual(context["model_input_context"]["context_records"], [])

    def test_model_projection_flattens_strict_case_ast_into_one_cpu_signature(self) -> None:
        code_unit = pasted_code_unit(
            "void AstcEncode(uint8_t *pixels, size_t count) { for (size_t i = 0; i < count; ++i) pixels[i] += 1; }",
            "AstcEncode",
            "AstcEncode",
        )
        strict_case = {
            "record_id": "astc_opencl_pair",
            "record_type": "strict_heterogeneous_case",
            "name": "ASTC OpenCL encode with CPU fallback",
            "algorithm": "astc_texture_encode",
            "target_unit": "GPU_OPENCL",
            "cpu_fallback_ast": {
                "binding_status": "exact_definition",
                "matchability": "ready",
                "compute_core": {"matching_status": "confirmed", "status": "ready", "reason": "loop body reviewed"},
                "function": {"qualified_name": "AstcSoftwareEncodeCore", "signature": "void AstcSoftwareEncodeCore()", "source_reference": "astc.cpp:42"},
                "ast_features": {
                    "parse_status": "complete",
                    "code_metrics": {"line_count": 80},
                    "algorithm_tags": ["block_encode"],
                    "types_used": ["uint8_t"],
                    "memory_access": {"pointer_expression": 2},
                    "loop_count": 2,
                    "branch_count": 1,
                    "max_loop_nesting": 2,
                },
            },
            "cpu_code_features": {
                "availability": "ast_available",
                "summary": "This long narrative is useful for audit but must not duplicate structured model evidence.",
                "matching_guidance": "This guidance belongs to the local tool contract, not every model request.",
                "structured_features": {
                    "binding_status": "exact_definition",
                    "compute_core_status": "confirmed",
                    "algorithm_tags": ["block_encode"],
                    "data_type_evidence": ["uint8_t"],
                    "memory_access": {"classification": ["contiguous"], "pointer_expression_count": 2},
                    "control_flow": {"loop_count": 2, "branch_count": 1, "max_loop_nesting": 2},
                    "computation": {"math_calls": [], "notable_callees": ["EncodeBlock"], "operators": ["+"]},
                    "parallelism": {"openmp_pragma_count": 0, "classification": "no_openmp_pragma_observed"},
                },
            },
        }
        context = prepare_context(
            "kb_rag",
            code_unit,
            [strict_case],
            [],
            [],
            optimization_kb_metadata={"kb_id": "optimization-test", "version": "test-v1", "content_sha256": "o" * 64},
            hardware_kb_metadata={"kb_id": "hardware-test", "version": "test-v1", "content_sha256": "h" * 64},
        )
        projected = context["model_input_context"]["context_records"][0]["record"]
        self.assertIn("cpu_ast_binding", projected)
        self.assertIn("cpu_signature", projected)
        self.assertNotIn("cpu_fallback_ast", projected)
        self.assertNotIn("cpu_code_features", projected)
        self.assertNotIn("summary", projected["cpu_signature"])
        self.assertEqual(projected["cpu_signature"]["control_flow"]["loop_count"], 2)
        self.assertEqual(projected["cpu_signature"]["memory_access"]["classification"], ["contiguous"])

    def test_result_cannot_cite_evidence_outside_its_context_pack(self) -> None:
        context = self._context("direct_baseline")
        result = {
            "schema_version": "opportunity_reasoning/v1",
            "recommendation": "candidate",
            "confidence": "medium",
            "evidence_status": "context_supported",
            "opportunities": [
                {
                    "opportunity_kind": "heterogeneous",
                    "candidate_target": "GPU_SHADER",
                    "evidence_ids": ["kb:sig_pixel_lut"],
                }
            ],
        }
        validated, errors = validate_reasoning_result(result, context)
        self.assertTrue(errors)
        self.assertEqual(validated["evidence_status"], "insufficient_evidence")
        self.assertEqual(validated["recommendation"], "evidence_required")

    def test_hypothesis_cannot_be_saved_as_an_evidence_backed_candidate(self) -> None:
        context = self._context("direct_baseline")
        result = {
            "schema_version": "opportunity_reasoning/v1",
            "recommendation": "candidate",
            "confidence": "medium",
            "evidence_status": "model_hypothesis",
            "summary": "The cache direction requires repeated input evidence.",
            "opportunities": [
                {
                    "opportunity_kind": "algorithmic",
                    "candidate_target": "local cache",
                    "evidence_ids": ["trace:pasted_code"],
                }
            ],
        }

        validated, errors = validate_reasoning_result(result, context)

        self.assertEqual(errors, [])
        self.assertEqual(validated["recommendation"], "evidence_required")
        self.assertEqual(validated["confidence"], "low")
        self.assertEqual(validated["evidence_status"], "insufficient_evidence")
        self.assertEqual(validated["evidence_calibration"]["status"], "conservative_downgrade")

    def test_direct_baseline_candidate_is_downgraded_even_when_it_cites_source_and_trace(self) -> None:
        context = self._context("direct_baseline")
        source_sha = str(self.code_unit["source"]["sha256"])
        result = {
            "schema_version": "opportunity_reasoning/v1",
            "recommendation": "candidate",
            "confidence": "medium",
            "evidence_status": "context_supported",
            "summary": "The source contains a loop and is a possible CPU candidate.",
            "opportunities": [
                {
                    "opportunity_kind": "parallel_cpu",
                    "candidate_target": "CPU thread pool",
                    "evidence_ids": ["trace:pasted_code", f"source:{source_sha}"],
                }
            ],
        }

        validated, errors = validate_reasoning_result(result, context)

        self.assertEqual(errors, [])
        self.assertEqual(validated["recommendation"], "evidence_required")
        self.assertEqual(validated["confidence"], "low")
        self.assertEqual(validated["evidence_status"], "insufficient_evidence")

    def test_common_loose_model_shape_is_adapted_conservatively(self) -> None:
        context = self._context("kb_rag")
        source_sha = str(self.code_unit["source"]["sha256"])
        loose = {
            "schema": "opportunity_reasoning/v1",
            "reasoning_summary": "The loop is data parallel but still needs device evidence.",
            "opportunities": {
                "heterogeneous": [
                    {
                        "target_unit": "GPU_SHADER",
                        "reasoning": "Independent pixels can be processed in parallel.",
                        "evidence_ids": ["kb:sig_pixel_lut", f"source:{source_sha}"],
                        "evidence_required": ["device capability probe"],
                    }
                ],
                "cpu_parallel": [
                    {
                        "target": "CPU thread pool",
                        "reasoning": "The iteration space can be chunked.",
                        "evidence_ids": [f"source:{source_sha}"],
                    }
                ],
            },
        }

        adapted, adaptation = adapt_reasoning_result(loose)
        validated, errors = validate_reasoning_result(adapted, context)

        self.assertIsNotNone(adaptation)
        self.assertEqual(validated["recommendation"], "evidence_required")
        self.assertEqual(validated["evidence_status"], "insufficient_evidence")
        self.assertEqual(validated["opportunities"][0]["opportunity_kind"], "heterogeneous")
        self.assertEqual(validated["opportunities"][1]["opportunity_kind"], "parallel_cpu")
        self.assertEqual(errors, [])

    def test_missing_analysis_scope_is_conservatively_declared_unknown(self) -> None:
        context = self._context("kb_rag")
        source_sha = str(self.code_unit["source"]["sha256"])
        result = {
            "schema_version": "opportunity_reasoning/v1",
            "recommendation": "candidate",
            "confidence": "medium",
            "evidence_status": "context_supported",
            "summary": "可见循环尚缺少设备和端到端证据。",
            "opportunities": [{
                "opportunity_kind": "heterogeneous",
                "candidate_target": "GPU_SHADER",
                "evidence_ids": [f"source:{source_sha}"],
            }],
        }

        adapted, adaptation = adapt_reasoning_result(result)
        validated, errors = validate_reasoning_result(adapted, context)

        self.assertIsNotNone(adaptation)
        self.assertEqual(adapted["opportunities"][0]["analysis_scope"]["level"], "unknown")
        self.assertEqual(adapted["opportunities"][0]["analysis_scope"]["status"], "not_declared")
        self.assertEqual(errors, [])
        self.assertEqual(validated["validation"]["status"], "valid")
        self.assertEqual(validated["recommendation"], "evidence_required")
        self.assertEqual(validated["evidence_status"], "insufficient_evidence")

    def test_declared_analysis_scope_must_use_the_controlled_enums(self) -> None:
        context = self._context("kb_rag")
        source_sha = str(self.code_unit["source"]["sha256"])
        result = {
            "schema_version": "opportunity_reasoning/v1",
            "recommendation": "evidence_required",
            "confidence": "low",
            "evidence_status": "insufficient_evidence",
            "summary": "可见循环尚缺少设备和端到端证据。",
            "opportunities": [{
                "opportunity_kind": "heterogeneous",
                "candidate_target": "GPU_SHADER",
                "evidence_ids": [f"source:{source_sha}"],
                "analysis_scope": {
                    "level": "made_up_scope",
                    "status": "observed",
                    "description": "虚构范围。",
                    "source_evidence": "没有可验证的源码依据。",
                },
            }],
        }

        _, errors = validate_reasoning_result(result, context)

        self.assertIn("unsupported analysis_scope level", errors)

    def test_adapter_never_preserves_more_than_three_loose_candidates(self) -> None:
        loose = {
            "candidates": [
                {"category": "cpu_parallel", "target": f"candidate-{index}", "reasoning": "parallel loop"}
                for index in range(4)
            ]
        }

        adapted, adaptation = adapt_reasoning_result(loose)

        self.assertIsNotNone(adaptation)
        self.assertEqual(len(adapted["opportunities"]), 3)
        self.assertIn("opportunities truncated to contract maximum of three", adaptation["actions"])

    def test_adapter_collects_root_level_category_emitted_by_model(self) -> None:
        loose = {
            "opportunities": {"heterogeneous": [{"target": "GPU", "reasoning": "pixel work"}]},
            "cpu_parallel": [{"target": "thread pool", "reasoning": "independent chunks"}],
        }

        adapted, _ = adapt_reasoning_result(loose)

        self.assertEqual([item["opportunity_kind"] for item in adapted["opportunities"]], ["heterogeneous", "parallel_cpu"])

    def test_primary_secondary_provider_shape_is_adapted_without_a_repair_turn(self) -> None:
        source_sha = str(self.code_unit["source"]["sha256"])
        loose = {
            "schema_version": "opportunity_reasoning/v1",
            "recommendation": "candidate",
            "confidence": "medium",
            "evidence_status": "context_supported",
            "primary_opportunity": {
                "unit_id": "gpu",
                "rationale": "可见循环按元素独立处理连续像素。",
                "candidate_apis": ["vkCmdDispatch", "glDispatchCompute"],
                "constraints": ["需要验证批量阈值"],
                "data_flow": "CPU buffer -> GPU buffer -> CPU buffer",
            },
            "secondary_opportunity": {
                "unit_id": "cpu",
                "rationale": "较小输入可使用 NEON 饱和运算。",
                "candidate_apis": ["ARM NEON SIMD"],
            },
            "supporting_evidence_ids": ["hardware:GPU_SHADER", f"source:{source_sha}", "trace:pasted_code"],
            "next_steps": ["比较 CPU、SIMD 和 GPU 的端到端时延"],
            "negative_evidence": [{"unit_id": "npu", "rationale": "单算子粒度过小。"}],
        }

        adapted, adaptation = adapt_reasoning_result(loose)

        self.assertIsNotNone(adaptation)
        self.assertEqual(len(adapted["opportunities"]), 2)
        self.assertEqual(adapted["opportunities"][0]["opportunity_kind"], "heterogeneous")
        self.assertIn("vkCmdDispatch", adapted["opportunities"][0]["candidate_target"])
        self.assertEqual(adapted["opportunities"][1]["opportunity_kind"], "simd")
        self.assertEqual(adapted["opportunities"][0]["evidence_ids"], ["hardware:GPU_SHADER", f"source:{source_sha}", "trace:pasted_code"])
        self.assertEqual(adapted["negative_evidence"], ["npu：单算子粒度过小。"])
        self.assertEqual(adapted["opportunities"][0]["verification_plan"], ["比较 CPU、SIMD 和 GPU 的端到端时延"])

    def test_simd_is_a_supported_opportunity_kind(self) -> None:
        context = self._context("kb_rag")
        source_sha = str(self.code_unit["source"]["sha256"])
        result = {
            "schema_version": "opportunity_reasoning/v1",
            "recommendation": "evidence_required",
            "confidence": "low",
            "evidence_status": "insufficient_evidence",
            "summary": "可见循环可评估 SIMD，但仍需数值语义与基准证据。",
            "opportunities": [{
                "opportunity_kind": "simd",
                "candidate_target": "ARM NEON",
                "analysis_scope": {
                    "level": "loop_region",
                    "status": "observed",
                    "description": "可见的逐元素循环。",
                    "source_evidence": "candidate scope loop_region_1_region",
                },
                "evidence_ids": [f"source:{source_sha}"],
            }],
            "missing_evidence": [],
            "negative_evidence": [],
        }

        _, errors = validate_reasoning_result(result, context)

        self.assertEqual(errors, [])

    def test_comparison_surfaces_shared_missing_evidence(self) -> None:
        comparison = compare_results(
            [
                {"strategy_id": "kb_rag", "result": {"recommendation": "candidate", "missing_evidence": ["device_probe", "benchmark"]}},
                {"strategy_id": "direct_baseline", "result": {"recommendation": "candidate", "missing_evidence": ["device_probe", "api_check"]}},
            ]
        )
        self.assertTrue(comparison["agreement"])
        self.assertEqual(comparison["shared_missing_evidence"], ["device_probe"])


if __name__ == "__main__":
    unittest.main()
