from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as platform_app
from backend.core import ArtifactStore
from backend.hardware_semantic_routing import (
    SEMANTIC_ROUTING_CONTEXT_VERSION,
    build_hardware_semantic_routing_context,
    validate_hardware_semantic_routing_result,
)
from backend.reasoning_lab import _model_selected_hardware_routes, pasted_code_unit


def _hardware(hardware_id: str, unit_type: str, **extra: object) -> dict[str, object]:
    return {
        "record_id": hardware_id,
        "record_type": "hardware_unit",
        "hardware_id": hardware_id,
        "unit_type": unit_type,
        "name": extra.pop("name", hardware_id),
        "software_interface": extra.pop("software_interface", "source-derived integration"),
        "supported_algorithms": extra.pop("supported_algorithms", []),
        "implementation_paths": extra.pop("implementation_paths", []),
        "constraints": extra.pop("constraints", []),
        **extra,
    }


def _profile() -> dict[str, object]:
    return {
        "schema_version": "target_platform_profile/v1",
        "profile_source": "user_selected",
        "capability_status": "not_device_probe",
        "chipset": {"family_id": "kirin", "family_label": "麒麟 SoC", "model_id": "kirin_9000", "model_label": "麒麟 9000"},
        "operating_system": {"family_id": "harmonyos", "family_label": "鸿蒙操作系统", "version_id": "harmonyos_5", "version_label": "HarmonyOS 5"},
    }


class HardwareSemanticRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            _hardware("NNRT_HDI_DEVICE", "neural_network_device", software_interface="OpenHarmony NNRT", supported_algorithms=["tensor graph operations"]),
            _hardware("GPU_VULKAN_RENDERING", "gpu", software_interface="Vulkan/OpenGL ES", supported_algorithms=["image pixel processing"]),
            _hardware("AUDIO_EFFECT_DSP_HDI", "audio_dsp", software_interface="Audio Effect HDI", supported_algorithms=["pcm effect processing"]),
            _hardware("HCODEC_HDI", "video_codec", software_interface="Codec Component HDI", supported_algorithms=["hardware video codec"]),
        ]

    def _context(self, code: str, function_hint: str = "") -> dict[str, object]:
        return build_hardware_semantic_routing_context(
            pasted_code_unit(code, function_hint, function_hint),
            self.records,
            target_platform_profile=_profile(),
        )

    def test_model_input_excludes_rule_route_and_unit_specific_feature_flags(self) -> None:
        # This resembles the user's renamed sample: it has data semantics that
        # are useful to a model, but intentionally lacks effect/DSP keywords.
        context = self._context(
            """for (std::size_t frame = 0; frame < frameCount; ++frame) {
              for (std::size_t channel = 0; channel < channelCount; ++channel) {
                const std::size_t index = frame * channelCount + channel;
                const float sample = pcm[index];
                const float equalized = sample * lowBandGain + sample * highBandGain;
                pcm[index] = Clamp(equalized);
              }
            }"""
        )

        model_context = context["model_input_context"]
        self.assertEqual(context["schema_version"], SEMANTIC_ROUTING_CONTEXT_VERSION)
        self.assertNotIn("deterministic_router_baseline", context)
        self.assertEqual(
            [card["unit_id"] for card in model_context["hardware_capability_cards"]],
            ["npu", "gpu", "dsp", "media"],
        )
        self.assertTrue(any(item.startswith("hardware:AUDIO_EFFECT_DSP_HDI") for item in model_context["allowed_evidence_ids"]))
        self.assertEqual(model_context["generic_code_profile"]["schema_version"], "generic_static_code_profile/v1")
        self.assertEqual(model_context["generic_code_profile"]["projection"], "model_safe_generic_code_profile/v1")
        self.assertEqual(model_context["candidate_scope_method"], "generic_lexical_scope_selection/v1")
        serialized = json.dumps(model_context, ensure_ascii=False)
        self.assertNotIn("deterministic_router", serialized)
        self.assertNotIn("semantic_observations", serialized)
        self.assertNotIn("audio_data", serialized)
        self.assertNotIn("convolution_terms", serialized)
        self.assertNotIn("function_hint", model_context)
        self.assertNotIn("source", model_context)
        self.assertNotIn("event_name", model_context["hotspot"])
        self.assertNotIn("module", model_context["hotspot"])
        self.assertNotIn("skill_profile", model_context)
        self.assertNotIn("ipc_or_remote_symbols", model_context["generic_code_profile"]["boundaries"])
        self.assertNotIn("synchronization_symbols", model_context["generic_code_profile"]["boundaries"])

    def test_production_context_does_not_execute_deterministic_router(self) -> None:
        # A raised baseline call would make this test fail.  The production
        # context must be buildable even when the legacy router is unavailable.
        with patch("backend.hardware_unit_routing.route_hardware_units", side_effect=AssertionError("legacy router executed")):
            context = self._context("void F_17(float *v, size_t n) { for (size_t i = 0; i < n; ++i) v[i] *= 0.5F; }", "F_17")
        self.assertNotIn("deterministic_router_baseline", context)

    def test_audit_baseline_is_explicit_and_never_projected_to_model(self) -> None:
        context = build_hardware_semantic_routing_context(
            pasted_code_unit("void F(float *v, size_t n) { for (size_t i = 0; i < n; ++i) v[i] *= 0.5F; }", "F", "F"),
            self.records,
            target_platform_profile=_profile(),
            include_deterministic_baseline=True,
        )
        self.assertIn("deterministic_router_baseline", context)
        self.assertNotIn("deterministic_router", json.dumps(context["model_input_context"], ensure_ascii=False))

    def test_a_model_can_return_dsp_for_renamed_pcm_loop_without_claiming_device_support(self) -> None:
        context = self._context(
            """for (std::size_t frame = 0; frame < frameCount; ++frame) {
              for (std::size_t channel = 0; channel < channelCount; ++channel) {
                const std::size_t index = frame * channelCount + channel;
                pcm[index] = Clamp(pcm[index] * lowBandGain + pcm[index] * highBandGain);
              }
            }"""
        )
        normalized, errors = validate_hardware_semantic_routing_result(
            {
                "schema_version": "hardware_semantic_routing/v2",
                "decision": "select_units",
                "summary": "可见 PCM 的逐帧逐通道浮点变换与 DSP 音频处理语义相近，但尚未证明 provider 或设备能力。",
                "semantic_profile": {
                    "data_representation": ["浮点数组上的连续下标读写"],
                    "operation_pattern": ["嵌套迭代中的逐元素乘加和钳位"],
                    "dependency_and_state": ["迭代独立性尚待验证"],
                    "boundary_conditions": ["未观察到 provider 或实时调度合同"],
                    "uncertainty": ["PCM 格式与帧大小未知"],
                },
                "candidates": [
                    {
                        "unit_id": "dsp",
                        "confidence": "medium",
                        "analysis_scope": "双层 frame/channel 循环中的 pcm[index] 逐样本计算",
                        "observed_code_evidence": ["frame/channel 双层循环", "pcm 数组读写", "float 乘加与钳位"],
                        "rationale": "它是规则逐帧逐通道 PCM 变换，可作为 Audio Effect HDI DSP 路径的待验证候选。",
                        "missing_evidence": ["PCM 格式与采样率", "effect provider", "frame latency", "AP fallback"],
                        "evidence_ids": ["hardware:AUDIO_EFFECT_DSP_HDI"],
                    }
                ],
                "negative_evidence": ["代码未显示 HDI provider 或实时调度合同"],
            },
            context,
        )

        self.assertEqual(errors, [])
        self.assertEqual(normalized["validation"]["status"], "valid")
        self.assertEqual(normalized["decision"], "select_units")
        self.assertEqual(normalized["candidates"][0]["unit_id"], "dsp")
        self.assertIn("浮点数组", normalized["semantic_profile"]["data_representation"][0])

    def test_fully_renamed_numeric_loop_can_stay_conservative(self) -> None:
        context = self._context(
            """for (std::size_t a = 0; a < outer; ++a) {
              for (std::size_t b = 0; b < inner; ++b) {
                const std::size_t p = a * inner + b;
                values[p] = Limit(values[p] * gain0 + values[p] * gain1);
              }
            }""",
            "RenamedWrapper",
        )
        normalized, errors = validate_hardware_semantic_routing_result(
            {
                "schema_version": "hardware_semantic_routing/v2",
                "decision": "needs_compute_core",
                "summary": "可见结构是通用浮点数组循环，删除领域数据语义后无法可靠区分 DSP、GPU 或其他计算路径。",
                "semantic_profile": {
                    "data_representation": ["浮点数组下标访问"],
                    "operation_pattern": ["双层逐元素算术循环"],
                    "dependency_and_state": ["迭代独立性未证明"],
                    "boundary_conditions": ["未观察到数据域或调用边界"],
                    "uncertainty": ["输入数据的真实语义未提供"],
                },
                "candidates": [],
                "negative_evidence": ["没有足以区分单元的领域数据合同"],
            },
            context,
        )
        self.assertEqual(errors, [])
        self.assertEqual(normalized["decision"], "needs_compute_core")

    def test_invalid_unit_and_evidence_are_rejected(self) -> None:
        context = self._context("void Proxy() { SendTransactCmd(); }")
        normalized, errors = validate_hardware_semantic_routing_result(
            {
                "decision": "select_units",
                "summary": "bad",
                "candidates": [{"unit_id": "fpga", "confidence": "certain", "evidence_ids": ["hardware:invented"]}],
            },
            context,
        )

        self.assertGreaterEqual(len(errors), 2)
        self.assertEqual(normalized["decision"], "needs_compute_core")
        self.assertEqual(normalized["candidates"], [])
        self.assertEqual(normalized["validation"]["status"], "invalid")

    def test_user_built_catalog_is_a_first_class_isolated_route_directory(self) -> None:
        kb_id = "custom_api_kb_" + "a" * 24
        unit_id = f"custom:{kb_id}"
        custom_record = {
            "record_id": kb_id,
            "record_type": "custom_hardware_api_directory",
            "hardware_id": kb_id,
            "routing_unit_id": unit_id,
            "routing_evidence_id": f"custom_api_kb:{kb_id}",
            "custom_api_kb_id": kb_id,
            "content_sha256": "c" * 64,
            "accelerator_unit": "AAE",
            "name": "AAE API（自建）",
            "software_interface": "5 条源码可追溯 API 合同候选",
            "supported_algorithms": ["PCM 音效处理", "执行"],
            "constraints": ["自建目录不是设备能力证明"],
            "representative_api_summaries": [{
                "record_id": "custom_api:aae_process",
                "symbol": "AAE_ProcessPcm",
                "semantic_family": {"id": "custom_execution", "label_zh": "执行"},
                "one_line_abstract_zh": "处理一帧 PCM 样本。",
                "expected_data_forms": ["PCM frame samples"],
            }],
        }
        context = build_hardware_semantic_routing_context(
            pasted_code_unit("void F(float *pcm, size_t n) { for (size_t i = 0; i < n; ++i) pcm[i] *= 0.5F; }", "F", "F"),
            [*self.records, custom_record],
            target_platform_profile=_profile(),
        )

        cards = context["model_input_context"]["hardware_capability_cards"]
        self.assertEqual([card["unit_id"] for card in cards], ["npu", "gpu", "dsp", "media", unit_id])
        custom_card = cards[-1]
        self.assertEqual(custom_card["catalog_origin"], "user_built")
        self.assertEqual(custom_card["allowed_evidence_ids"], [f"custom_api_kb:{kb_id}"])
        self.assertEqual(context["routing_catalog_summary"]["custom_unit_count"], 1)

        normalized, errors = validate_hardware_semantic_routing_result(
            {
                "decision": "select_units",
                "summary": "将自建 AAE 目录作为待验证的 API 检索入口。",
                "semantic_profile": {
                    "data_representation": ["PCM 浮点样本"],
                    "operation_pattern": ["逐样本音效变换"],
                    "dependency_and_state": ["迭代依赖待验证"],
                    "boundary_conditions": ["provider 未知"],
                    "uncertainty": ["设备可用性未知"],
                },
                "candidates": [{
                    "unit_id": unit_id,
                    "confidence": "medium",
                    "analysis_scope": "PCM 数组循环",
                    "observed_code_evidence": ["浮点数组逐元素处理"],
                    "rationale": "可见计算与 AAE 目录中的 PCM 处理合同语义相容。",
                    "missing_evidence": ["provider 和设备 capability"],
                    "evidence_ids": [f"custom_api_kb:{kb_id}"],
                }],
                "negative_evidence": ["自建目录未证明设备支持"],
            },
            context,
        )
        self.assertEqual(errors, [])
        self.assertEqual(normalized["candidates"][0]["unit_id"], unit_id)
        routes, routing = _model_selected_hardware_routes(normalized, [*self.records, custom_record])
        self.assertEqual(routes[0]["evidence_id"], f"custom_api_kb:{kb_id}")
        self.assertEqual(routing["primary_unit_id"], unit_id)

    def test_candidate_must_cite_a_capability_card_for_its_own_unit(self) -> None:
        context = self._context("void F(float *v, size_t n) { for (size_t i = 0; i < n; ++i) v[i] *= 0.5F; }")
        normalized, errors = validate_hardware_semantic_routing_result(
            {
                "decision": "select_units",
                "summary": "错误地把通用循环映射为 DSP。",
                "candidates": [{
                    "unit_id": "dsp",
                    "confidence": "low",
                    "analysis_scope": "可见循环",
                    "observed_code_evidence": ["float 数组循环"],
                    "rationale": "待验证。",
                    "evidence_ids": ["hardware:GPU_VULKAN_RENDERING"],
                }],
            },
            context,
        )
        self.assertTrue(any("不属于该单元能力卡" in error for error in errors))
        self.assertEqual(normalized["validation"]["status"], "invalid")

    def test_ipc_model_can_select_no_accelerator_without_candidates(self) -> None:
        context = self._context("bool Proxy() { MessageParcel data; data.WriteInt32(1); return SendTransactCmd(1, data); }")
        normalized, errors = validate_hardware_semantic_routing_result(
            {
                "decision": "no_accelerator_candidate",
                "summary": "可见代码是同步 IPC 包装，不是计算核心。",
                "candidates": [],
                "negative_evidence": ["无规则数据并行计算", "SendTransactCmd 是外部调用边界"],
            },
            context,
        )

        self.assertEqual(errors, [])
        self.assertEqual(normalized["decision"], "no_accelerator_candidate")

    def test_manual_directory_selection_is_auditable_and_not_relabelled_as_model_evidence(self) -> None:
        code_unit = pasted_code_unit(
            "void Adjust(float *values, size_t count) { for (size_t i = 0; i < count; ++i) values[i] *= 0.5F; }",
            "Adjust",
            "Adjust",
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            arguments = {
                "source_connector_id": "connector_test",
                "source_binding": {"declared_revision": None, "resolved_snapshot_id": "snapshot_test", "match_level": "unknown", "resolver_version": "1.0"},
                "producer": {"name": "test", "version": "1.0"},
            }
            code_artifact = store.write("hotspot_code_unit", code_unit, **arguments)
            context_payload = build_hardware_semantic_routing_context(
                code_artifact,
                self.records,
                target_platform_profile=_profile(),
            )
            context = store.write("hardware_semantic_routing_context", context_payload, **arguments)
            model_result, errors = validate_hardware_semantic_routing_result(
                {
                    "decision": "no_accelerator_candidate",
                    "summary": "当前可见循环缺少领域数据语义。",
                    "semantic_profile": {"uncertainty": ["需要人工复核目录"]},
                    "candidates": [],
                    "negative_evidence": ["未形成模型单元候选"],
                },
                context,
            )
            self.assertEqual(errors, [])
            parent = store.write(
                "hardware_semantic_routing_result",
                {"context_artifact_id": context["artifact_id"], "result": model_result, "validation_errors": []},
                **arguments,
            )
            with patch.object(platform_app, "store", store):
                response = platform_app.save_hardware_semantic_routing_manual_selection(
                    "local-default",
                    platform_app.HardwareSemanticRoutingManualSelectionRequest(
                        result_artifact_id=parent["artifact_id"],
                        unit_id="dsp",
                        note="人工核对 DSP 音频 API 目录。",
                    ),
                )

            selected = store.read("hardware_semantic_routing_result", response["artifact"]["artifact_id"])
            result = selected["result"]
            self.assertEqual(result["selection_origin"], "user_manual")
            self.assertEqual(result["selection_note"], "人工核对 DSP 音频 API 目录。")
            self.assertEqual(result["validation"]["status"], "valid")
            self.assertEqual(result["candidates"][0]["unit_id"], "dsp")
            routes, routing = _model_selected_hardware_routes(result, self.records)
            self.assertEqual(len(routes), 1)
            self.assertEqual(routing["method"], "user_manual_hardware_directory/v1")
            self.assertEqual(routing["selection_origin"], "user_manual")

    def test_direct_route_api_and_parallel_selections_create_valid_audit_artifacts(self) -> None:
        code_unit = pasted_code_unit(
            "void Adjust(float *values, size_t count) { for (size_t i = 0; i < count; ++i) values[i] *= 0.5F; }",
            "Adjust",
            "Adjust",
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            arguments = {
                "source_connector_id": "connector_test",
                "source_binding": {"declared_revision": None, "resolved_snapshot_id": "snapshot_test", "match_level": "unknown", "resolver_version": "1.0"},
                "producer": {"name": "test", "version": "1.0"},
            }
            code_artifact = store.write("hotspot_code_unit", code_unit, **arguments)
            gpu_record_id = str(platform_app._accelerator_api_kb("gpu")["records"][0]["record_id"])
            with (
                patch.object(platform_app, "store", store),
                patch.object(platform_app, "_hardware_reasoning_records", return_value=self.records),
            ):
                route = platform_app.save_hardware_semantic_routing_direct_selection(
                    "local-default",
                    platform_app.HardwareSemanticRoutingDirectSelectionRequest(
                        code_artifact_id=code_artifact["artifact_id"],
                        unit_id="gpu",
                        target_platform_profile=platform_app.TargetPlatformProfileRequest.model_validate(_profile()),
                    ),
                )
                api_selection = platform_app.save_manual_opportunity_selection(
                    "local-default",
                    platform_app.ManualOpportunitySelectionRequest(
                        code_artifact_id=code_artifact["artifact_id"],
                        selection_kind="hardware_api",
                        unit_id="gpu",
                        api_record_id=gpu_record_id,
                        hardware_semantic_routing_result_artifact_id=route["artifact"]["artifact_id"],
                        target_platform_profile=platform_app.TargetPlatformProfileRequest.model_validate(_profile()),
                    ),
                )
                parallel_selection = platform_app.save_manual_opportunity_selection(
                    "local-default",
                    platform_app.ManualOpportunitySelectionRequest(
                        code_artifact_id=code_artifact["artifact_id"],
                        selection_kind="parallel_cpu",
                        target_platform_profile=platform_app.TargetPlatformProfileRequest.model_validate(_profile()),
                    ),
                )

            self.assertEqual(route["artifact"]["result"]["selection_origin"], "user_manual")
            self.assertIsNone(route["artifact"]["manual_selection"]["parent_result_artifact_id"])
            self.assertEqual(route["artifact"]["result"]["validation"]["status"], "valid")
            self.assertEqual(api_selection["artifact"]["manual_selection"]["selected_unit"], "gpu")
            self.assertEqual(api_selection["artifact"]["result"]["validation"]["status"], "valid")
            self.assertEqual(parallel_selection["artifact"]["manual_selection"]["selected_unit"], "parallel_cpu")
            self.assertEqual(parallel_selection["artifact"]["result"]["opportunities"][0]["opportunity_kind"], "parallel_cpu")
            self.assertEqual(parallel_selection["artifact"]["result"]["validation"]["status"], "valid")

    def test_direct_custom_route_and_api_selection_remain_in_the_same_catalog(self) -> None:
        kb_id = "custom_api_kb_" + "d" * 24
        unit_id = f"custom:{kb_id}"
        api_record = {
            "record_id": "custom_api:aae_process_pcm",
            "record_type": "custom_accelerator_api_candidate",
            "accelerator_unit": "AAE",
            "symbol": "AAE_ProcessPcm",
            "one_line_abstract_zh": "处理一帧 PCM 浮点样本。",
            "semantic_family": {"id": "custom_execution", "label_zh": "PCM 执行"},
            "code_feature_match": {"expected_data_forms": ["PCM frame samples"]},
        }
        routing_record = {
            "record_id": kb_id,
            "record_type": "custom_hardware_api_directory",
            "hardware_id": kb_id,
            "routing_unit_id": unit_id,
            "routing_evidence_id": f"custom_api_kb:{kb_id}",
            "custom_api_kb_id": kb_id,
            "content_sha256": "d" * 64,
            "accelerator_unit": "AAE",
            "name": "AAE API（自建）",
            "supported_algorithms": ["PCM 音效处理"],
            "constraints": ["需设备验证"],
            "representative_api_summaries": [{
                "record_id": api_record["record_id"],
                "symbol": api_record["symbol"],
                "semantic_family": api_record["semantic_family"],
                "one_line_abstract_zh": api_record["one_line_abstract_zh"],
                "expected_data_forms": ["PCM frame samples"],
            }],
        }
        catalog = {
            "unit_id": unit_id,
            "kb_id": kb_id,
            "metadata": {"kb_id": kb_id, "content_sha256": "d" * 64},
            "routing_record": routing_record,
            "records": [api_record],
        }
        code_unit = pasted_code_unit(
            "void Adjust(float *pcm, size_t count) { for (size_t i = 0; i < count; ++i) pcm[i] *= 0.5F; }",
            "Adjust",
            "Adjust",
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            arguments = {
                "source_connector_id": "connector_test",
                "source_binding": {"declared_revision": None, "resolved_snapshot_id": "snapshot_test", "match_level": "unknown", "resolver_version": "1.0"},
                "producer": {"name": "test", "version": "1.0"},
            }
            code_artifact = store.write("hotspot_code_unit", code_unit, **arguments)
            with (
                patch.object(platform_app, "store", store),
                patch.object(platform_app, "_hardware_reasoning_records", return_value=self.records),
                patch.object(platform_app, "_custom_api_reasoning_catalogs", return_value={unit_id: catalog}),
                patch.object(platform_app.custom_api_kbs, "record", return_value={"record": api_record}),
            ):
                route = platform_app.save_hardware_semantic_routing_direct_selection(
                    "local-default",
                    platform_app.HardwareSemanticRoutingDirectSelectionRequest(
                        code_artifact_id=code_artifact["artifact_id"],
                        unit_id=unit_id,
                        target_platform_profile=platform_app.TargetPlatformProfileRequest.model_validate(_profile()),
                    ),
                )
                selection = platform_app.save_manual_opportunity_selection(
                    "local-default",
                    platform_app.ManualOpportunitySelectionRequest(
                        code_artifact_id=code_artifact["artifact_id"],
                        selection_kind="hardware_api",
                        unit_id=unit_id,
                        api_record_id=str(api_record["record_id"]),
                        hardware_semantic_routing_result_artifact_id=route["artifact"]["artifact_id"],
                        target_platform_profile=platform_app.TargetPlatformProfileRequest.model_validate(_profile()),
                    ),
                )

            self.assertEqual(route["artifact"]["result"]["candidates"][0]["unit_id"], unit_id)
            self.assertEqual(route["artifact"]["result"]["candidates"][0]["evidence_ids"], [f"custom_api_kb:{kb_id}"])
            self.assertEqual(selection["artifact"]["manual_selection"]["selected_unit"], unit_id)
            self.assertIn("custom_api_record:custom_api:aae_process_pcm", selection["artifact"]["result"]["opportunities"][0]["evidence_ids"])


if __name__ == "__main__":
    unittest.main()
