from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from backend.application_flow import ApplicationFlowManager
from backend.application_flow_trace import ApplicationFlowRuntimeTraceManager
from backend.core import ArtifactStore, PlatformError, SourceConnectorManager


class ApplicationFlowManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.source_root = self.root / "camera-module"
        source = self.source_root / "src" / "camera_flow.cpp"
        source.parent.mkdir(parents=True)
        source.write_text(
            """void StartCamera()
{
    HITRACE_METER_NAME(\"CameraStart\");
    ConfigureSession();
}

void ConfigureSession()
{
    OpenPreviewStream();
}

void OnPreviewReady()
{
    HITRACE_METER_NAME(\"PreviewReady\");
    NotifyUi();
}
""",
            encoding="utf-8",
        )
        (source.parent / "CameraFlowView.tsx").write_text(
            "export function CameraFlowView() { return <div>camera</div> }\n",
            encoding="utf-8",
        )
        generated = self.source_root / "doxygen_output" / "html" / "camera_flow.cpp.js"
        generated.parent.mkdir(parents=True)
        generated.write_text("CameraStart generated documentation\n", encoding="utf-8")
        instrumentation = self.source_root / "instrumentation"
        instrumentation.mkdir()
        (instrumentation / "effect_trace.h").write_text(
            "#define EFFECT_TRACE_NAME(name) StartTrace(HITRACE_TAG_ZIMAGE, name)\n",
            encoding="utf-8",
        )
        (instrumentation / "effect.cpp").write_text(
            "void RenderEffect()\n{\n    EFFECT_TRACE_NAME(\"ImageEffect::Render\");\n}\n",
            encoding="utf-8",
        )
        (instrumentation / "trace_wrapper.cpp").write_text(
            "void Trace_Start(const char *name)\n"
            "{\n"
            "    OH_HiTrace_StartTrace(name);\n"
            "}\n\n"
            "void BeginOperation()\n"
            "{\n"
            "    Trace_Start(\"WrappedOperation\");\n"
            "}\n",
            encoding="utf-8",
        )
        self.gallery_source_root = self.root / "gallery-module"
        gallery_source = self.gallery_source_root / "src" / "gallery_flow.ets"
        gallery_source.parent.mkdir(parents=True)
        gallery_source.write_text(
            "export function CompleteGallerySave() {\n"
            "  hiTraceMeter.startAsyncTrace('GallerySave', 1)\n"
            "  NotifyGallery()\n"
            "}\n",
            encoding="utf-8",
        )
        self.store = ArtifactStore(self.state_root, project_id="local-default")
        self.connectors = SourceConnectorManager(self.state_root, self.store, workspace_root=self.root)
        self.connector = {
            "connector_id": "local_camera_test",
            "kind": "local",
            "mode": "local_readonly",
            "display_name": "Camera module",
            "source_root": str(self.source_root.resolve()),
            "snapshot": {"snapshot_id": "source_snapshot_camera_v1", "clean": True, "repositories": []},
            "worktree_fingerprint": {"sha256": "a" * 64},
        }
        self.gallery_connector = {
            "connector_id": "local_gallery_test",
            "kind": "local",
            "mode": "local_readonly",
            "display_name": "Gallery module",
            "source_root": str(self.gallery_source_root.resolve()),
            "snapshot": {"snapshot_id": "source_snapshot_gallery_v1", "clean": True, "repositories": []},
            "worktree_fingerprint": {"sha256": "b" * 64},
        }
        self.connectors.registry.write({"connectors": [self.connector, self.gallery_connector]})
        self.skill_root = (
            Path(__file__).resolve().parents[2]
            / "agent-resources"
            / "skills"
            / "application-execution-flow-reconstruction"
        )
        self.manager = ApplicationFlowManager(
            self.connectors,
            self.store,
            scenario_schema_path=self.skill_root / "references" / "application-flow-scenario-v2.schema.json",
            discovery_schema_path=self.skill_root / "references" / "application-flow-discovery-v1.schema.json",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def discovery(self) -> dict[str, object]:
        return {
            "schema_version": "application_flow_discovery/v1",
            "source_connector_id": "local_camera_test",
            "source_snapshot_id": "source_snapshot_camera_v1",
            "scope": "src",
            "analysis_summary": {
                "what": "从公开入口到预览就绪回调重建相机启动流程。",
                "scope_rationale": "测试只覆盖 camera-module/src。",
                "methods": ["入口检索", "调用点阅读", "Trace 插桩核对"],
            },
            "flows": [
                {
                    "id": "camera_startup",
                    "name": "相机启动",
                    "order": 1,
                    "description": "从 StartCamera 进入，配置会话并等待预览就绪。",
                    "trigger": "用户打开相机。",
                    "completion": "UI 收到预览就绪通知。",
                    "participants": ["camera_app", "camera_session"],
                    "entry_points": [
                        {
                            "id": "entry_start_camera",
                            "name": "启动入口",
                            "symbol": "StartCamera",
                            "kind": "public_entry",
                            "source_evidence_ids": ["ev_start_camera"],
                        }
                    ],
                    "exit_points": [
                        {
                            "id": "exit_preview_ready",
                            "name": "预览就绪",
                            "symbol": "OnPreviewReady",
                            "kind": "callback",
                            "source_evidence_ids": ["ev_preview_ready"],
                        }
                    ],
                    "source_evidence": [
                        {
                            "id": "ev_start_camera",
                            "path": "src/camera_flow.cpp",
                            "line_start": 1,
                            "line_end": 5,
                            "symbol": "StartCamera",
                            "evidence_kind": "entry",
                            "claim": "入口调用 ConfigureSession，并含 CameraStart 插桩。",
                        },
                        {
                            "id": "ev_preview_ready",
                            "path": "src/camera_flow.cpp",
                            "line_start": 12,
                            "line_end": 16,
                            "symbol": "OnPreviewReady",
                            "evidence_kind": "exit",
                            "claim": "回调含 PreviewReady 插桩并通知 UI。",
                        },
                    ],
                    "trace_anchor_candidates": [
                        {
                            "id": "anchor_camera_start",
                            "name": "CameraStart",
                            "kind": "hitrace_literal",
                            "process_name": "camera_app",
                            "source_evidence_ids": ["ev_start_camera"],
                            "verification_status": "observed_in_source",
                            "source_location": {"path": "src/camera_flow.cpp", "line": 3},
                            "instrumentation": {
                                "macro": "HITRACE_METER_NAME",
                                "event_label_match": "exact_literal",
                                "build_condition": "unconditional",
                            },
                        },
                        {
                            "id": "anchor_preview_ready",
                            "name": "PreviewReady",
                            "kind": "hitrace_literal",
                            "process_name": "camera_app",
                            "source_evidence_ids": ["ev_preview_ready"],
                            "verification_status": "observed_in_source",
                            "source_location": {"path": "src/camera_flow.cpp", "line": 14},
                            "instrumentation": {
                                "macro": "HITRACE_METER_NAME",
                                "event_label_match": "exact_literal",
                                "build_condition": "unconditional",
                            },
                        },
                    ],
                    "confidence": "high",
                    "open_questions": ["真实 Trace 中是否经过异步服务回调仍需采集验证。"],
                }
            ],
            "unknowns": ["当前只有源码可达证据，没有场景 Trace。"],
        }

    def test_flow_confirmation_preference_defaults_true_and_persists(self) -> None:
        self.assertTrue(self.manager.get_preferences()["confirm_flow_before_trace"])

        updated = self.manager.update_preferences(confirm_flow_before_trace=False)
        catalog = self.manager.list_assets()
        restored = ApplicationFlowManager(
            self.connectors,
            self.store,
            scenario_schema_path=self.skill_root / "references" / "application-flow-scenario-v2.schema.json",
            discovery_schema_path=self.skill_root / "references" / "application-flow-discovery-v1.schema.json",
        )

        self.assertFalse(updated["confirm_flow_before_trace"])
        self.assertFalse(catalog["preferences"]["confirm_flow_before_trace"])
        self.assertFalse(restored.get_preferences()["confirm_flow_before_trace"])

    @staticmethod
    def scenario() -> dict[str, object]:
        return {
            "version": "2.0",
            "scenario": "camera_startup",
            "description": "相机启动到预览就绪。",
            "source_mermaid": "camera_startup.mermaid",
            "constants": {"RELATION_RANGE_NS": 5000000000},
            "process_groups": {"application": ["camera_app"]},
            "stage_search_window": {"source": "trace"},
            "stages": [
                {
                    "id": "camera_boot",
                    "name": "启动并等待预览",
                    "process_name": "camera_app",
                    "order": 1,
                    "optional": False,
                    "start": {
                        "type": "trace_point",
                        "name": "CameraStart",
                        "name_match": "exact",
                        "which": "start",
                        "process": {"process_group": "application"},
                    },
                    "end": {
                        "type": "trace_point",
                        "name": "PreviewReady",
                        "name_match": "exact",
                        "which": "end",
                        "process": {"process_group": "application"},
                    },
                }
            ],
            "relations": [],
            "relation_defaults": {},
        }

    @staticmethod
    def mermaid() -> str:
        return """%% @scenario camera_startup version=2.0
%% @stage camera_boot order=1 process=camera_app required name="启动并等待预览"
graph LR
    S1[启动并等待预览]
"""

    @staticmethod
    def compilable_mermaid() -> str:
        return """%% @scenario camera_startup version=2.0
%% @description 相机启动到预览就绪。
%% @process_group application=camera_app
%% @stage camera_boot order=1 process=camera_app required name="启动并等待预览"
%%   start: trace_point "CameraStart" exact in application
%%   end: trace_point "PreviewReady" exact in application
graph LR
    S1[启动并等待预览]
"""

    def save_discovery(self) -> dict[str, object]:
        return self.manager.save_discovery(
            "local_camera_test",
            self.discovery(),
            subpath="src",
        )

    def write_function_derived_trace_fixture(self) -> None:
        include = self.source_root / "include" / "camera_trace.h"
        include.parent.mkdir(parents=True, exist_ok=True)
        include.write_text(
            "#define CAMERA_SYNC_TRACE HITRACE_METER_NAME(HITRACE_TAG_ZCAMERA, __PRETTY_FUNCTION__)\n",
            encoding="utf-8",
        )
        source = self.source_root / "src" / "hcamera_service.cpp"
        source.write_text(
            """#include \"camera_trace.h\"

int32_t HCameraService::CreateCaptureSession(sptr<ICaptureSession>& session, int32_t opMode)
{
    CAMERA_SYNC_TRACE;
    return 0;
}
""",
            encoding="utf-8",
        )

    def discovery_with_function_derived_anchor(self, *, name: str, label_match: str) -> dict[str, object]:
        self.write_function_derived_trace_fixture()
        discovery = self.discovery()
        flow = discovery["flows"][0]
        flow["source_evidence"].extend([
            {
                "id": "ev_create_session",
                "path": "src/hcamera_service.cpp",
                "line_start": 3,
                "line_end": 6,
                "symbol": "int32_t HCameraService::CreateCaptureSession(sptr<ICaptureSession>& session, int32_t opMode)",
                "evidence_kind": "trace_instrumentation",
                "claim": "CreateCaptureSession 作用域调用 CAMERA_SYNC_TRACE。",
            },
            {
                "id": "ev_camera_trace_macro",
                "path": "include/camera_trace.h",
                "line_start": 1,
                "line_end": 1,
                "symbol": "CAMERA_SYNC_TRACE",
                "evidence_kind": "trace_instrumentation",
                "claim": "包装宏最终把 __PRETTY_FUNCTION__ 作为 event label。",
            },
        ])
        flow["trace_anchor_candidates"].append({
            "id": "anchor_create_session",
            "name": name,
            "kind": "function_scope_sync_trace",
            "process_name": "camera_app",
            "source_evidence_ids": ["ev_create_session", "ev_camera_trace_macro"],
            "verification_status": "observed_in_source",
            "source_location": {"path": "src/hcamera_service.cpp", "line": 5},
            "instrumentation": {
                "macro": "CAMERA_SYNC_TRACE",
                "event_label_match": label_match,
                "build_condition": "HITRACE_TAG_ZCAMERA enabled",
            },
        })
        return discovery

    def test_discovers_and_saves_line_verified_flow(self) -> None:
        inspection = self.manager.inspect_repository("local_camera_test", subpath="src")
        search = self.manager.search_source(
            "local_camera_test",
            subpath="src",
            query="PreviewReady",
        )
        saved = self.save_discovery()
        catalog = self.manager.list_assets(source_connector_id="local_camera_test")

        self.assertEqual(inspection["scan"]["files_considered"], 2)
        self.assertIn(".tsx", {item["suffix"] for item in inspection["language_file_counts"]})
        self.assertIn("StartCamera", {item["symbol"] for item in inspection["action_symbol_candidates"]})
        self.assertIn("OnPreviewReady", {item["symbol"] for item in inspection["action_symbol_candidates"]})
        self.assertIn(14, {item["line"] for item in search["matches"]})
        self.assertEqual(saved["artifact"]["quality"]["source_evidence_count"], 2)
        self.assertEqual(catalog["discoveries"][0]["flows"][0]["id"], "camera_startup")

    def test_compact_candidate_catalog_expands_into_strict_discovery(self) -> None:
        graph_artifact = self.store.write(
            "module_code_graph",
            {
                "graph_id": "mcg_" + "b" * 24,
                "graph_contract": "module_code_graph/v1",
                "scope": "src",
                "source_snapshot_id": "source_snapshot_camera_v1",
                "summary": {"files_parsed": 2},
            },
            source_connector_id="local_camera_test",
            source_binding={"resolved_snapshot_id": "source_snapshot_camera_v1", "match_level": "exact"},
            producer={"name": "test-code-graph", "version": "1.0.0"},
        )
        saved = self.manager.save_candidate_catalog(
            "local_camera_test",
            subpath="src",
            summary_zh="发现相机启动到预览就绪候选流程。",
            analysis_goal="分析用户打开相机到预览首帧就绪。",
            code_graph_artifact_id=graph_artifact["artifact_id"],
            trace_analysis_artifact_id="art_trace_camera_startup",
            candidates=[{
                "name": "相机启动",
                "description": "从公开启动入口到预览就绪回调。",
                "trigger": "用户请求启动相机。",
                "completion": "预览就绪回调通知 UI。",
                "operation_scope": "complete_user_operation",
                "boundary_rationale": "从用户打开相机到首个预览画面可见，覆盖完整可观察操作。",
                "user_actions": [{
                    "order": 1,
                    "name": "打开相机",
                    "description": "用户从应用入口打开相机页面。",
                    "observable_outcome": "预览首帧出现。",
                }],
                "semantic_outline": ["接收打开请求", "建立相机会话", "发布预览首帧"],
                "participants": ["camera_app", "camera_session"],
                "entry": {"path": "src/camera_flow.cpp", "line": 1, "symbol": "StartCamera"},
                "exit": {"path": "src/camera_flow.cpp", "line": 12, "symbol": "OnPreviewReady"},
                "confidence": "medium",
                "open_questions": ["实现调用链留待选中后闭合。"],
            }],
            unknowns=["阶段一未搜索 HiTrace。"],
        )

        artifact = self.manager.read_discovery(saved["artifact"]["artifact_id"])
        discovery = artifact["discovery"]
        flow = discovery["flows"][0]
        self.assertEqual(saved["catalog_contract"], "application_flow_candidate_catalog/v2")
        self.assertEqual(flow["id"], "flow_01")
        self.assertEqual(len(flow["source_evidence"]), 2)
        self.assertEqual(flow["trace_anchor_candidates"], [])
        self.assertEqual(flow["entry_points"][0]["source_evidence_ids"], ["ev_flow_01_entry"])
        self.assertEqual(artifact["analysis_context"]["analysis_goal"], "分析用户打开相机到预览首帧就绪。")
        self.assertEqual(artifact["analysis_context"]["trace_analysis_artifact_id"], "art_trace_camera_startup")
        listed = self.manager.list_assets(source_connector_id="local_camera_test")
        listed_discovery = next(item for item in listed["discoveries"] if item["artifact_id"] == artifact["artifact_id"])
        self.assertEqual(listed_discovery["trace_analysis_artifact_id"], "art_trace_camera_startup")
        self.assertEqual(artifact["analysis_context"]["interaction_state"], "candidate_confirmation_required")
        self.assertEqual(artifact["analysis_context"]["candidate_blueprints"][0]["user_actions"][0]["name"], "打开相机")
        self.assertIn(graph_artifact["artifact_id"], artifact["input_artifacts"])

    def test_compact_candidate_catalog_rejects_symbol_not_present_near_line(self) -> None:
        with self.assertRaises(PlatformError) as raised:
            self.manager.save_candidate_catalog(
                "local_camera_test",
                subpath="src",
                summary_zh="候选流程。",
                candidates=[{
                    "name": "错误候选",
                    "description": "边界符号与源码不一致。",
                    "trigger": "测试触发。",
                    "completion": "测试完成。",
                    "operation_scope": "complete_user_operation",
                    "boundary_rationale": "覆盖测试触发到测试完成。",
                    "user_actions": [{"order": 1, "name": "触发", "description": "执行测试触发。", "observable_outcome": "测试完成。"}],
                    "semantic_outline": ["执行测试"],
                    "entry": {"path": "src/camera_flow.cpp", "line": 1, "symbol": "InventedEntry"},
                    "exit": {"path": "src/camera_flow.cpp", "line": 12, "symbol": "OnPreviewReady"},
                }],
            )

        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID")

    def test_candidate_catalog_preserves_cross_repository_boundaries_and_action_gaps(self) -> None:
        saved = self.manager.save_candidate_catalog(
            "local_camera_test",
            subpath="src",
            summary_zh="发现跨相机与图库模块的完整保存操作。",
            analysis_goal="拍摄并保存后在图库中看到结果。",
            source_scopes=[
                {
                    "source_connector_id": "local_camera_test",
                    "source_snapshot_id": "source_snapshot_camera_v1",
                    "subpath": "src",
                },
                {
                    "source_connector_id": "local_gallery_test",
                    "source_snapshot_id": "source_snapshot_gallery_v1",
                    "subpath": "src",
                },
            ],
            candidates=[{
                "name": "拍摄并写入图库",
                "description": "从相机启动入口跨模块到图库保存完成。",
                "trigger": "用户触发拍摄。",
                "completion": "图库收到保存完成通知。",
                "operation_scope": "complete_user_operation",
                "boundary_rationale": "覆盖用户触发到结果可在图库观察的完整边界。",
                "user_actions": [
                    {
                        "order": 1,
                        "name": "打开相机",
                        "description": "进入相机并开始准备。",
                        "observable_outcome": "相机开始配置。",
                    },
                    {
                        "order": 2,
                        "name": "保存照片",
                        "description": "保存拍摄结果。",
                        "observable_outcome": "图库出现结果。",
                    },
                ],
                "action_coverage": [
                    {
                        "action_order": 1,
                        "mapping_status": "mapped",
                        "source_connector_ids": ["local_camera_test"],
                        "evidence_summary": "StartCamera 提供相机侧入口。",
                    },
                    {
                        "action_order": 2,
                        "mapping_status": "partial",
                        "source_connector_ids": ["local_gallery_test"],
                        "evidence_summary": "CompleteGallerySave 提供图库侧完成通知。",
                        "missing_reason": "媒体写盘服务不在当前两个代码仓中。",
                        "suspected_modules": ["媒体库写盘服务"],
                    },
                ],
                "semantic_outline": ["相机准备", "跨模块保存", "图库完成通知"],
                "participants": ["camera", "gallery"],
                "entry": {
                    "source_connector_id": "local_camera_test",
                    "path": "src/camera_flow.cpp",
                    "line": 1,
                    "symbol": "StartCamera",
                },
                "exit": {
                    "source_connector_id": "local_gallery_test",
                    "path": "src/gallery_flow.ets",
                    "line": 1,
                    "symbol": "CompleteGallerySave",
                },
            }],
        )

        artifact = self.manager.read_discovery(saved["artifact"]["artifact_id"])
        flow = artifact["discovery"]["flows"][0]
        blueprint = artifact["analysis_context"]["candidate_blueprints"][0]
        self.assertEqual(len(artifact["analysis_context"]["source_scopes"]), 2)
        self.assertEqual(
            flow["source_evidence"][1]["path"],
            "@source/local_gallery_test/src/gallery_flow.ets",
        )
        self.assertEqual(blueprint["coverage_summary"]["partial_count"], 1)
        self.assertEqual(blueprint["action_coverage"][1]["suspected_modules"], ["媒体库写盘服务"])
        self.assertTrue(any("媒体库写盘服务" in item for item in artifact["discovery"]["unknowns"]))
        filtered = self.manager.list_assets(source_connector_id="local_gallery_test")
        self.assertIn(saved["artifact"]["artifact_id"], [item["artifact_id"] for item in filtered["discoveries"]])

    def test_trace_instrumentation_inspector_follows_module_macro_and_arkts_wrapper(self) -> None:
        cpp_result = self.manager.inspect_trace_instrumentation(
            "local_camera_test",
            path="instrumentation/effect.cpp",
            line=1,
            symbol="RenderEffect",
            radius=40,
        )
        macro = next(item for item in cpp_result["candidates"] if item["call"] == "EFFECT_TRACE_NAME")
        self.assertEqual(macro["boundary_role"], "scope")
        self.assertEqual(macro["wrapper_resolution_status"], "resolved_to_hitrace_base")
        self.assertIn("StartTrace", macro["wrapper_chain"][0]["base_hitrace_calls"])
        self.assertIn("ImageEffect::Render", macro["event_label_candidates"])

        wrapper_result = self.manager.inspect_trace_instrumentation(
            "local_camera_test",
            path="instrumentation/trace_wrapper.cpp",
            line=6,
            symbol="BeginOperation",
            radius=40,
        )
        wrapper = next(
            item for item in wrapper_result["candidates"]
            if item["call"] == "Trace_Start" and item["line"] == 8
        )
        self.assertEqual(wrapper["wrapper_resolution_status"], "resolved_to_hitrace_base")
        self.assertTrue(any(item["kind"] == "wrapper_definition_candidate" for item in wrapper["wrapper_chain"]))

        ets_result = self.manager.inspect_trace_instrumentation(
            "local_gallery_test",
            path="src/gallery_flow.ets",
            line=1,
            symbol="CompleteGallerySave",
            radius=40,
        )
        arkts = next(item for item in ets_result["candidates"] if item["call"] == "hiTraceMeter.startAsyncTrace")
        self.assertEqual(arkts["boundary_role"], "start")
        self.assertEqual(arkts["instrumentation_family"], "direct_hitrace_api")
        self.assertIn("GallerySave", arkts["event_label_candidates"])

    def test_candidate_refinement_preserves_parent_lineage_and_complete_operation_contract(self) -> None:
        candidate = {
            "name": "相机完整启动",
            "description": "从用户打开相机到预览首帧可见。",
            "trigger": "用户打开相机页面。",
            "completion": "预览首帧显示。",
            "operation_scope": "complete_user_operation",
            "boundary_rationale": "以用户可观察的页面进入和首帧显示为边界。",
            "user_actions": [{
                "order": 1,
                "name": "打开相机",
                "description": "用户进入相机页面。",
                "observable_outcome": "预览画面出现。",
            }],
            "semantic_outline": ["接收页面进入", "建立预览会话", "显示预览首帧"],
            "participants": ["camera_app"],
            "entry": {"path": "src/camera_flow.cpp", "line": 1, "symbol": "StartCamera"},
            "exit": {"path": "src/camera_flow.cpp", "line": 12, "symbol": "OnPreviewReady"},
        }
        first = self.manager.save_candidate_catalog(
            "local_camera_test",
            subpath="src",
            summary_zh="发现完整相机启动操作。",
            analysis_goal="打开相机直到预览可见。",
            candidates=[candidate],
        )["artifact"]

        refined_candidate = copy.deepcopy(candidate)
        refined_candidate["boundary_rationale"] = "用户确认完成边界是预览首帧，而不是会话创建成功。"
        second = self.manager.save_candidate_catalog(
            "local_camera_test",
            subpath="src",
            summary_zh="按用户反馈校准完成边界。",
            analysis_goal="打开相机直到预览可见。",
            parent_discovery_artifact_id=first["artifact_id"],
            refinement_request="完成边界必须是预览首帧可见。",
            candidates=[refined_candidate],
        )["artifact"]

        artifact = self.manager.read_discovery(second["artifact_id"])
        context = artifact["analysis_context"]
        self.assertEqual(context["interaction_iteration"], 2)
        self.assertEqual(context["parent_discovery_artifact_id"], first["artifact_id"])
        self.assertEqual(context["refinement_request"], "完成边界必须是预览首帧可见。")
        self.assertIn(first["artifact_id"], artifact["input_artifacts"])

        invalid = copy.deepcopy(candidate)
        invalid["operation_scope"] = "internal_step"
        with self.assertRaises(PlatformError) as raised:
            self.manager.save_candidate_catalog(
                "local_camera_test",
                subpath="src",
                summary_zh="错误地把内部步骤当成流程。",
                candidates=[invalid],
            )
        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID")

    def test_candidate_catalog_accepts_more_than_three_evidence_backed_flows(self) -> None:
        candidates = [
            {
                "name": f"业务流程 {index}",
                "description": f"由源码证据支持的第 {index} 个独立业务流程。",
                "trigger": f"触发条件 {index}。",
                "completion": f"完成边界 {index}。",
                "operation_scope": "complete_user_operation",
                "boundary_rationale": f"覆盖第 {index} 个操作从触发到可观察完成。",
                "user_actions": [{"order": 1, "name": f"执行操作 {index}", "description": f"触发第 {index} 个操作。", "observable_outcome": f"观察到结果 {index}。"}],
                "semantic_outline": [f"处理业务流程 {index}"],
                "participants": ["camera_app"],
                "entry": {"path": "src/camera_flow.cpp", "line": 1, "symbol": "StartCamera"},
                "exit": {"path": "src/camera_flow.cpp", "line": 12, "symbol": "OnPreviewReady"},
            }
            for index in range(1, 13)
        ]

        saved = self.manager.save_candidate_catalog(
            "local_camera_test",
            subpath="src",
            summary_zh="按真实业务边界识别多个候选流程。",
            candidates=candidates,
        )

        discovery = self.manager.read_discovery(saved["artifact"]["artifact_id"])["discovery"]
        self.assertEqual(len(discovery["flows"]), 12)
        self.assertEqual(discovery["flows"][-1]["id"], "flow_12")

        with self.assertRaises(PlatformError) as raised:
            self.manager.save_candidate_catalog(
                "local_camera_test",
                subpath="src",
                summary_zh="超过单次有界候选上限。",
                candidates=(candidates * 3)[:33],
            )
        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID")

    def test_repository_inspection_bounds_model_visible_candidates(self) -> None:
        for index in range(36):
            directory = self.source_root / "src" / f"module_{index:02d}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "BUILD.gn").write_text("source_set(\"sample\") {}\n", encoding="utf-8")
            (directory / f"camera_entry_{index:02d}.cpp").write_text(
                f"void CameraEntry{index}() {{}}\n",
                encoding="utf-8",
            )

        inspection = self.manager.inspect_repository("local_camera_test", subpath="src")

        self.assertEqual(len(inspection["manifest_candidates"]), 16)
        self.assertEqual(len(inspection["entry_file_candidates"]), 24)
        self.assertEqual(inspection["candidate_counts"]["manifest_total"], 36)
        self.assertGreaterEqual(inspection["candidate_counts"]["entry_file_total"], 36)
        self.assertTrue(inspection["output_truncated"]["manifest_candidates"])
        self.assertTrue(inspection["output_truncated"]["entry_file_candidates"])

    def test_lightweight_discovery_accepts_empty_trace_anchor_candidates(self) -> None:
        discovery = self.discovery()
        discovery["flows"][0]["trace_anchor_candidates"] = []

        saved = self.manager.save_discovery("local_camera_test", discovery, subpath="src")

        self.assertEqual(saved["artifact"]["discovery"]["flows"][0]["trace_anchor_candidates"], [])

    def test_source_search_accepts_a_file_scope_and_skips_doxygen_output(self) -> None:
        file_search = self.manager.search_source(
            "local_camera_test",
            subpath="src/camera_flow.cpp",
            query="CameraStart",
        )
        repository_search = self.manager.search_source(
            "local_camera_test",
            query="CameraStart",
        )

        self.assertEqual(file_search["scope"], "src/camera_flow.cpp")
        self.assertEqual({item["path"] for item in file_search["matches"]}, {"src/camera_flow.cpp"})
        self.assertNotIn("doxygen_output/html/camera_flow.cpp.js", {item["path"] for item in repository_search["matches"]})

    def test_schema_rejects_renamed_attribute_and_order_gap(self) -> None:
        renamed = self.discovery()
        flow = renamed["flows"][0]
        flow["label"] = flow.pop("name")
        with self.assertRaises(PlatformError) as raised:
            self.manager.validate_discovery("local_camera_test", renamed, expected_scope="src")
        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_DISCOVERY_INVALID")

        interrupted = self.discovery()
        interrupted["flows"][0]["order"] = 2
        with self.assertRaises(PlatformError) as raised:
            self.manager.validate_discovery("local_camera_test", interrupted, expected_scope="src")
        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_DISCOVERY_INVALID")

    def test_source_trace_anchor_must_match_the_declared_source_window(self) -> None:
        discovery = self.discovery()
        discovery["flows"][0]["trace_anchor_candidates"][0]["name"] = "InventedCameraEvent"

        with self.assertRaises(PlatformError) as raised:
            self.manager.validate_discovery("local_camera_test", discovery, expected_scope="src")

        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_TRACE_ANCHOR_INVALID")

    def test_function_derived_anchor_uses_stable_selector_not_source_declaration(self) -> None:
        discovery = self.discovery_with_function_derived_anchor(
            name="HCameraService::CreateCaptureSession",
            label_match="function_derived",
        )

        validated = self.manager.validate_discovery("local_camera_test", discovery, expected_scope="src")

        anchor = validated["flows"][0]["trace_anchor_candidates"][-1]
        self.assertEqual(anchor["name"], "HCameraService::CreateCaptureSession")
        self.assertEqual(anchor["instrumentation"]["event_label_match"], "function_derived")

    def test_function_declaration_cannot_masquerade_as_wrapper_literal_event(self) -> None:
        discovery = self.discovery_with_function_derived_anchor(
            name="int32_t HCameraService::CreateCaptureSession(sptr<ICaptureSession>& session, int32_t opMode)",
            label_match="wrapper_literal",
        )

        with self.assertRaises(PlatformError) as raised:
            self.manager.validate_discovery("local_camera_test", discovery, expected_scope="src")

        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_TRACE_ANCHOR_INVALID")
        self.assertIn("字符串字面量", raised.exception.message)

    def test_saves_ready_scenario_and_preserves_reference_field_names(self) -> None:
        discovery = self.save_discovery()["artifact"]
        saved = self.manager.save_scenario(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            scenario_json=self.scenario(),
            mermaid=self.mermaid(),
            stage_evidence=[
                {
                    "stage_id": "camera_boot",
                    "source_evidence_ids": ["ev_start_camera", "ev_preview_ready"],
                    "trace_anchor_status": "observed_in_source",
                    "notes": "开始与结束事件字面量均在源码中观察到。",
                    "boundary_anchors": [
                        {"boundary": "start", "anchor_id": "anchor_camera_start"},
                        {"boundary": "end", "anchor_id": "anchor_preview_ready"},
                    ],
                }
            ],
            unknowns=["尚未用真实 Trace 验证阶段时序。"],
        )

        artifact = saved["artifact"]
        self.assertEqual(artifact["quality"]["executable_status"], "ready")
        self.assertEqual(
            set(artifact["scenario_json"]),
            {
                "version", "scenario", "description", "source_mermaid", "constants",
                "process_groups", "stage_search_window", "stages", "relations", "relation_defaults",
                "nodes", "node_relations",
            },
        )
        self.assertEqual(artifact["quality"]["source_declared_anchor_count"], 2)
        self.assertEqual(artifact["stage_evidence"][0]["boundary_anchors"][0]["boundary"], "start")

    def test_compiles_mermaid_as_the_only_model_authored_scenario_input(self) -> None:
        discovery = self.save_discovery()["artifact"]

        result = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=self.compilable_mermaid(),
        )

        artifact = result["artifact"]
        scenario = artifact["scenario_json"]
        self.assertEqual(result["compiler"]["input_representation"], "mermaid_only")
        self.assertEqual(artifact["generation_method"], "deterministic_mermaid_compiler/v1")
        self.assertEqual(scenario["scenario"], "camera_startup")
        self.assertEqual(scenario["process_groups"], {"application": ["camera_app"]})
        self.assertEqual(scenario["stage_search_window"], {"source": "trace"})
        self.assertEqual(scenario["stages"][0]["start"]["name"], "CameraStart")
        self.assertEqual(scenario["stages"][0]["end"]["name"], "PreviewReady")
        self.assertEqual(
            [item["type"] for item in scenario["nodes"]],
            ["action", "phase", "stage"],
        )
        self.assertEqual(
            next(item for item in scenario["nodes"] if item["type"] == "stage")["parent_id"],
            "phase_01",
        )
        self.assertEqual(artifact["quality"]["executable_status"], "ready")
        self.assertEqual(
            [item["anchor_id"] for item in artifact["stage_evidence"][0]["boundary_anchors"]],
            ["anchor_camera_start", "anchor_preview_ready"],
        )
        self.assertEqual(artifact["stage_hierarchy"]["representation"], "legacy_flat_projection")

    def test_compiler_normalizes_agent_transport_and_metadata_variants(self) -> None:
        discovery = self.save_discovery()["artifact"]
        mermaid = """```mermaid
%% @scenario {"id":"invented_id","version":"application-flow-scenario/v2"}
%% @description 用户打开相机到预览首帧可见。
%% @process_group application=camera_app
%% @process_group application=camera_worker
%% @action action_open order=1 name="打开相机"
%% @phase phase_start action=action_open order=1 name="启动会话"
%% @stage camera_boot order=1 process=camera_app required name="启动并等待预览"
%%   start: trace_point "CameraStart" exact in application
%%   end: trace_point "PreviewReady" exact in application
%% @stage_hierarchy stage=camera_boot action=action_open phase=phase_start
%% @activity start_camera stage=camera_boot order=1 name="启动入口"
%% @activity configure_session stage=camera_boot order=2 name="配置会话"
%% @activity_relation from=start_camera to=configure_session type=sequence
%% @implementation stage=camera_boot activity=start_camera order=1 symbol=StartCamera path=src/camera_flow.cpp line=1
%% @implementation stage=camera_boot activity=configure_session order=1 symbol=ConfigureSession path=src/camera_flow.cpp line=7
flowchart LR
    A[启动入口] --> B[配置会话]
```
"""

        result = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=mermaid,
        )

        artifact = result["artifact"]
        self.assertTrue(artifact["mermaid"].startswith("%% @scenario camera_startup version=2.0\n"))
        self.assertNotIn("```", artifact["mermaid"])
        self.assertEqual(artifact["scenario_json"]["process_groups"], {"application": ["camera_app", "camera_worker"]})
        self.assertEqual(
            [item["activity_id"] for item in artifact["stage_hierarchy"]["implementations"]],
            ["start_camera", "configure_session"],
        )
        self.assertEqual(
            result["compiler"]["normalizations"],
            ["markdown_code_fence_removed", "scenario_metadata_canonicalized"],
        )

    def test_process_group_metadata_accepts_common_agent_forms(self) -> None:
        cases = {
            "application=camera_app,camera_worker": ("application", ["camera_app", "camera_worker"]),
            "application=image_editing processes=photo_picker,image_effect": (
                "image_editing",
                ["photo_picker", "image_effect"],
            ),
            "process_group=image_editing process=photo_picker process=image_effect": (
                "image_editing",
                ["photo_picker", "image_effect"],
            ),
            "image_editing photo_picker,image_effect": ("image_editing", ["photo_picker", "image_effect"]),
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual(
                    self.manager._parse_process_group_metadata(body, line_number=1),
                    expected,
                )

    def test_runtime_trace_catalog_preserves_same_name_uploads_and_supports_deletion(self) -> None:
        runtime = ApplicationFlowRuntimeTraceManager(self.state_root, self.store, self.manager)
        traces = []
        for index in (1, 2):
            traces.append(self.store.write(
                "trace_analysis_result",
                {
                    "input_kind": "external_trace_analysis_report",
                    "trace_name": "same-name.json",
                    "trace_import": {
                        "import_instance_id": f"trace_import_{index}",
                        "uploaded_at": f"2026-09-04T00:00:0{index}Z",
                    },
                    "quality": {"status": "partial"},
                },
                source_connector_id="local_camera_test",
                source_binding={"match_level": "unknown"},
                producer={"name": "test", "version": "1.0.0"},
            ))

        catalog = runtime.list_traces(source_connector_ids=["local_camera_test"])
        self.assertEqual({item["artifact_id"] for item in catalog["traces"]}, {item["artifact_id"] for item in traces})
        self.assertEqual({item["trace_name"] for item in catalog["traces"]}, {"same-name.json"})
        self.assertEqual(
            {item["uploaded_at"] for item in catalog["traces"]},
            {"2026-09-04T00:00:01Z", "2026-09-04T00:00:02Z"},
        )

        binding = runtime.bind_trace("application-flow:test", traces[1]["artifact_id"])
        self.assertEqual(binding["trace"]["artifact_id"], traces[1]["artifact_id"])
        self.assertEqual(runtime.resolve_bound_trace("application-flow:test"), traces[1]["artifact_id"])

        deleted = runtime.delete_trace(traces[1]["artifact_id"])
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(runtime.resolve_bound_trace("application-flow:test"), "")
        remaining = runtime.list_traces(source_connector_ids=["local_camera_test"])
        self.assertEqual([item["artifact_id"] for item in remaining["traces"]], [traces[0]["artifact_id"]])
        with self.assertRaises(PlatformError) as raised:
            runtime.read_trace(traces[1]["artifact_id"])
        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_TRACE_NOT_FOUND")

    def test_real_trace_alignment_uses_observed_event_ids_without_changing_scenario_schema(self) -> None:
        discovery = self.save_discovery()["artifact"]
        scenario = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=self.compilable_mermaid(),
        )["artifact"]
        trace = self.store.write(
            "trace_analysis_result",
            {
                "input_kind": "external_trace_analysis_report",
                "trace_name": "camera-runtime.json",
                "analysis_profile": "trace-team-stage-report-v1",
                "stage_catalog": [
                    {
                        "stage_id": "runtime_camera_boot",
                        "time_range_ns": {"start_ns": 1_000_000_000, "end_ns": 1_520_000_000},
                        "reported_event_count": 27,
                        "reported_total_event_duration_ms": 175.94,
                        "start_event": {
                            "name": "CameraStart",
                            "process_name": "camera_app",
                            "thread_name": "main",
                            "ts_ms": 1000.0,
                            "dur_ms": 0.01,
                        },
                        "end_event": {
                            "name": "PreviewReady",
                            "process_name": "camera_app",
                            "thread_name": "main",
                            "ts_ms": 1520.0,
                            "dur_ms": 0.01,
                        },
                        "top_events": [
                            {
                                "name": "CameraPipelineHeavyWork",
                                "process_name": "camera_app",
                                "thread_name": "worker",
                                "ts_ms": 1200.0,
                                "dur_ms": 120.0,
                            }
                        ],
                    }
                ],
                "hotspot_candidates": [
                    {
                        "trace_event_name": "CameraStart",
                        "module": "camera_app",
                        "metrics": {"occurrence_count": 1, "inclusive_total_duration_ms": 0.01},
                        "stage_context": {"stage_id": "runtime_camera_boot"},
                        "observed_occurrences": [
                            {
                                "name": "CameraStart",
                                "process_name": "camera_app",
                                "thread_name": "main",
                                "ts_ms": 1000.0,
                                "dur_ms": 0.01,
                            }
                        ],
                    }
                ],
                "quality": {"status": "trace_team_supplied"},
            },
            source_connector_id="local_camera_test",
            source_binding={"resolved_snapshot_id": "source_snapshot_camera_v1", "match_level": "exact"},
            producer={"name": "test-trace-import", "version": "1.0.0"},
        )
        runtime = ApplicationFlowRuntimeTraceManager(self.state_root, self.store, self.manager)

        inspection = runtime.inspect_trace(
            analysis_artifact_id=trace["artifact_id"],
            scenario_artifact_id=scenario["artifact_id"],
            query="相机启动到预览就绪",
        )
        events = {item["event_name"]: item for item in inspection["runtime_events"]}
        self.assertEqual(events["CameraStart"]["metrics"]["occurrence_count"], 1)
        self.assertEqual(events["PreviewReady"]["metrics"]["occurrence_count"], 1)
        result = runtime.apply_alignment(
            source_connector_id="local_camera_test",
            analysis_artifact_id=trace["artifact_id"],
            event_catalog_artifact_id=inspection["event_catalog_artifact_id"],
            scenario_artifact_id=scenario["artifact_id"],
            flow_id="camera_startup",
            mermaid=self.compilable_mermaid(),
            stage_matches=[
                {
                    "stage_id": "camera_boot",
                    "match_status": "matched",
                    "confidence": "high",
                    "start_event_id": events["CameraStart"]["event_id"],
                    "end_event_id": events["PreviewReady"]["event_id"],
                    "supporting_event_ids": [],
                    "rationale": "真实 Trace 中观察到启动与预览就绪边界，时序与场景一致。",
                }
            ],
            change_summary="使用真实事件名校准现有阶段边界。",
            limitations=["报告只包含阶段边界事件，不等价于原始 Trace 全量目录。"],
        )

        artifact = result["artifact"]
        self.assertEqual(
            set(artifact["scenario_json"]),
            {
                "version", "scenario", "description", "source_mermaid", "constants",
                "process_groups", "stage_search_window", "stages", "relations", "relation_defaults",
                "nodes", "node_relations",
            },
        )
        self.assertNotIn("runtime_trace_alignment", artifact["scenario_json"])
        self.assertNotIn("stage_analysis", artifact["scenario_json"])
        self.assertEqual(artifact["runtime_trace_alignment"]["quality"]["status"], "complete")
        stage_analysis = artifact["stage_analysis"]
        self.assertEqual(stage_analysis["schema_version"], "application_flow_stage_analysis/v1")
        self.assertEqual(stage_analysis["summary"]["total_stages"], 1)
        self.assertEqual(stage_analysis["summary"]["timed_stages"], 1)
        self.assertEqual(stage_analysis["summary"]["total_nodes"], 3)
        self.assertEqual(stage_analysis["summary"]["timed_nodes"], 3)
        self.assertAlmostEqual(stage_analysis["summary"]["trace_window_duration_ms"], 520.0)
        self.assertEqual(stage_analysis["summary"]["ranking_method"], "report_supplied_stage_top_events")
        self.assertEqual(stage_analysis["stages"][0]["id"], "camera_boot")
        self.assertEqual(
            [item["type"] for item in stage_analysis["nodes"]],
            ["action", "phase", "stage"],
        )
        self.assertEqual(stage_analysis["nodes"][0]["boundary_source"], "derived_from_children")
        self.assertEqual(stage_analysis["stages"][0]["coverage"], "aligned_event_evidence")
        self.assertEqual(stage_analysis["stages"][0]["event_count"], 27)
        self.assertAlmostEqual(stage_analysis["stages"][0]["total_event_duration_ms"], 175.94)
        self.assertAlmostEqual(stage_analysis["stages"][0]["duration_ms"], 520.0)
        self.assertEqual(
            [item["name"] for item in stage_analysis["stages"][0]["top_events"]],
            ["CameraPipelineHeavyWork"],
        )
        event_timeline = stage_analysis["stages"][0]["event_timeline"]
        self.assertEqual(event_timeline["representation"], "representative_occurrences")
        self.assertEqual(event_timeline["source_occurrence_count"], 1)
        self.assertEqual(event_timeline["returned_occurrence_count"], 1)
        self.assertFalse(event_timeline["truncated"])
        self.assertEqual(result["stage_analysis"], stage_analysis)
        self.assertEqual(artifact["quality"]["runtime_trace_observed_stage_count"], 1)
        self.assertEqual(artifact["parent_scenario_artifact_id"], scenario["artifact_id"])
        self.assertEqual(artifact["generation_method"], "runtime_trace_calibrated_mermaid/v1")
        self.assertEqual(artifact["scenario_json"]["stages"][0]["start"]["name"], "CameraStart")
        self.assertEqual(artifact["scenario_json"]["stages"][0]["start"]["name_match"], "exact")
        catalog_scenarios = {
            item["artifact_id"]: item
            for item in self.manager.list_assets(source_connector_id="local_camera_test")["scenarios"]
        }
        self.assertFalse(catalog_scenarios[scenario["artifact_id"]]["has_stage_analysis"])
        self.assertTrue(catalog_scenarios[artifact["artifact_id"]]["has_stage_analysis"])
        self.assertTrue(catalog_scenarios[artifact["artifact_id"]]["has_runtime_trace_alignment"])
        self.assertEqual(
            catalog_scenarios[artifact["artifact_id"]]["runtime_trace_analysis_artifact_id"],
            trace["artifact_id"],
        )

        with self.assertRaises(PlatformError) as inexact:
            runtime.apply_alignment(
                source_connector_id="local_camera_test",
                analysis_artifact_id=trace["artifact_id"],
                event_catalog_artifact_id=inspection["event_catalog_artifact_id"],
                scenario_artifact_id=scenario["artifact_id"],
                flow_id="camera_startup",
                mermaid=self.compilable_mermaid().replace(
                    'trace_point "CameraStart" exact',
                    'trace_point "CameraStart" substring_case_insensitive',
                ),
                stage_matches=[
                    {
                        "stage_id": "camera_boot",
                        "match_status": "matched",
                        "confidence": "high",
                        "start_event_id": events["CameraStart"]["event_id"],
                        "end_event_id": events["PreviewReady"]["event_id"],
                        "supporting_event_ids": [],
                        "rationale": "真实名称不能使用模糊匹配。",
                    }
                ],
                change_summary="非法模糊校准。",
                limitations=[],
            )
        self.assertEqual(inexact.exception.code, "APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID")

        with self.assertRaises(PlatformError) as raised:
            runtime.apply_alignment(
                source_connector_id="local_camera_test",
                analysis_artifact_id=trace["artifact_id"],
                event_catalog_artifact_id=inspection["event_catalog_artifact_id"],
                scenario_artifact_id=scenario["artifact_id"],
                flow_id="camera_startup",
                mermaid=self.compilable_mermaid(),
                stage_matches=[
                    {
                        "stage_id": "camera_boot",
                        "match_status": "matched",
                        "confidence": "high",
                        "start_event_id": "event_htrace_runtime_forged",
                        "end_event_id": events["PreviewReady"]["event_id"],
                        "supporting_event_ids": [],
                        "rationale": "伪造事件不得通过。",
                    }
                ],
                change_summary="非法校准。",
                limitations=[],
            )
        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID")

    def test_stage_analysis_projects_activity_evidence_into_parent_envelopes(self) -> None:
        runtime = ApplicationFlowRuntimeTraceManager(self.state_root, self.store, self.manager)
        scenario = self.scenario()
        scenario["nodes"] = [
            {
                "id": "action_open_camera", "type": "action", "parent_id": None,
                "name": "打开相机", "description": "打开相机", "order": 1,
                "process_name": None, "optional": False, "boundary_source": "derived_from_children",
                "start": scenario["stages"][0]["start"], "end": scenario["stages"][0]["end"],
            },
            {
                "id": "phase_start", "type": "phase", "parent_id": "action_open_camera",
                "name": "启动预览", "description": "启动预览", "order": 1,
                "process_name": None, "optional": False, "boundary_source": "derived_from_children",
                "start": scenario["stages"][0]["start"], "end": scenario["stages"][0]["end"],
            },
            {
                "id": "camera_boot", "type": "stage", "parent_id": "phase_start",
                "name": "启动相机", "description": "启动相机", "order": 1,
                "process_name": "camera_app", "optional": False, "boundary_source": "declared",
                "start": scenario["stages"][0]["start"], "end": scenario["stages"][0]["end"],
            },
            {
                "id": "prepare_camera", "type": "activity", "parent_id": "camera_boot",
                "name": "准备相机", "description": "准备相机", "order": 1,
                "process_name": "camera_app", "optional": False, "boundary_source": "runtime_observed",
                "start": None, "end": None,
            },
            {
                "id": "start_preview", "type": "activity", "parent_id": "camera_boot",
                "name": "启动预览", "description": "启动预览", "order": 2,
                "process_name": "camera_app", "optional": False, "boundary_source": "runtime_observed",
                "start": None, "end": None,
            },
        ]
        scenario["node_relations"] = [
            {"from": "prepare_camera", "to": "start_preview", "type": "sequence"}
        ]

        def runtime_event(event_id: str, name: str, start_ns: int, end_ns: int) -> dict:
            return {
                "event_id": event_id,
                "event_name": name,
                "metrics": {
                    "first_start_ns": start_ns,
                    "last_end_ns": end_ns,
                    "occurrence_count": 1,
                    "inclusive_total_duration_ms": (end_ns - start_ns) / 1_000_000,
                    "max_span_duration_ms": (end_ns - start_ns) / 1_000_000,
                },
                "trace_context": {
                    "process_name": "camera_app", "thread_name": "main", "pid": 10, "tid": 11,
                },
            }

        stage_start = runtime_event("event_stage_start", "CameraStart", 1_000_000_000, 1_001_000_000)
        stage_end = runtime_event("event_stage_end", "PreviewReady", 1_200_000_000, 1_201_000_000)
        prepare_scope = runtime_event("event_prepare", "PrepareCamera", 1_010_000_000, 1_050_000_000)
        preview_scope = runtime_event("event_preview", "StartPreview", 1_060_000_000, 1_150_000_000)
        analysis = runtime._build_stage_analysis(
            analysis={
                "artifact_id": "art_1234567890abcdef12345678",
                "input_kind": "external_trace_analysis_report",
                "trace_name": "hierarchy.json",
                "stage_catalog": [],
            },
            scenario=scenario,
            stage_matches=[{
                "stage_id": "camera_boot", "match_status": "matched", "confidence": "high",
                "start_event": stage_start, "end_event": stage_end, "supporting_events": [],
            }],
            node_matches=[
                {
                    "node_id": "prepare_camera", "match_status": "matched", "confidence": "high",
                    "start_event": prepare_scope, "end_event": prepare_scope, "supporting_events": [],
                },
                {
                    "node_id": "start_preview", "match_status": "matched", "confidence": "high",
                    "start_event": preview_scope, "end_event": preview_scope, "supporting_events": [],
                },
            ],
        )

        self.manager._validate_schema(
            "stage_analysis",
            analysis,
            code="APPLICATION_FLOW_STAGE_ANALYSIS_INVALID",
        )
        nodes = {item["id"]: item for item in analysis["nodes"]}
        self.assertEqual(analysis["summary"]["node_type_counts"]["activity"], 2)
        self.assertAlmostEqual(nodes["prepare_camera"]["duration_ms"], 40.0)
        self.assertEqual(nodes["prepare_camera"]["start_event"]["event_id"], "event_prepare")
        self.assertEqual(nodes["prepare_camera"]["end_event"]["event_id"], "event_prepare")
        self.assertEqual(nodes["prepare_camera"]["start_edge"], "start")
        self.assertEqual(nodes["prepare_camera"]["end_edge"], "end")
        self.assertAlmostEqual(nodes["phase_start"]["duration_ms"], 201.0)
        self.assertEqual(nodes["phase_start"]["boundary_source"], "derived_from_children")

    def test_compiles_hierarchical_authoring_metadata_without_changing_scenario_schema(self) -> None:
        discovery = self.save_discovery()["artifact"]
        mermaid = """%% @scenario camera_startup version=2.0
%% @description 用户打开相机到预览首帧可见。
%% @process_group application=camera_app
%% @action {"id":"action_open_camera","order":1,"name":"打开相机","description":"用户进入相机并等待预览"}
%% @phase {"id":"phase_start_session","action_id":"action_open_camera","order":1,"name":"启动相机会话","description":"创建并配置预览会话"}
%% @stage camera_boot order=1 process=camera_app required name="启动并等待预览"
%%   start: trace_point "CameraStart" exact in application
%%   end: trace_point "PreviewReady" exact in application
%% @stage_hierarchy {"stage_id":"camera_boot","action_id":"action_open_camera","phase_id":"phase_start_session"}
%% @activity {"id":"start_camera","stage_id":"camera_boot","order":1,"name":"启动相机入口","description":"接收用户启动请求"}
%% @activity {"id":"configure_session","stage_id":"camera_boot","order":2,"name":"配置预览会话","description":"建立预览流配置"}
%% @activity_relation {"from":"start_camera","to":"configure_session","type":"sequence","description":"入口同步调用配置"}
%% @implementation {"stage_id":"camera_boot","activity_id":"start_camera","order":1,"symbol":"StartCamera","path":"src/camera_flow.cpp","line":1,"relation":"entry"}
%% @implementation {"stage_id":"camera_boot","activity_id":"configure_session","order":2,"symbol":"ConfigureSession","path":"src/camera_flow.cpp","line":7,"relation":"call"}
flowchart LR
    subgraph A1["动作 1 · 打开相机"]
        subgraph P1["启动相机会话"]
            S1["启动并等待预览"]
        end
    end
"""

        result = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=mermaid,
        )

        artifact = result["artifact"]
        self.assertEqual(result["compiler"]["schema_version"], "application_flow_mermaid_compiler/v2")
        self.assertEqual(artifact["stage_hierarchy"]["representation"], "hierarchical_authoring_projection")
        self.assertEqual(artifact["stage_hierarchy"]["actions"][0]["id"], "action_open_camera")
        self.assertEqual(artifact["stage_hierarchy"]["phases"][0]["stage_ids"], ["camera_boot"])
        self.assertEqual(
            [item["id"] for item in artifact["stage_hierarchy"]["activities"]],
            ["start_camera", "configure_session"],
        )
        self.assertEqual(artifact["stage_hierarchy"]["activity_relations"][0]["type"], "sequence")
        self.assertEqual(
            [item["symbol"] for item in artifact["stage_hierarchy"]["implementations"]],
            ["StartCamera", "ConfigureSession"],
        )
        self.assertEqual(
            [item["activity_id"] for item in artifact["stage_hierarchy"]["implementations"]],
            ["start_camera", "configure_session"],
        )
        self.assertEqual(artifact["stage_hierarchy"]["quality"]["activity_count"], 2)
        self.assertEqual(artifact["quality"]["hierarchy_status"], "source_bound_hierarchy")
        granularity = artifact["stage_hierarchy"]["granularity_observation"]
        self.assertEqual(granularity["structural_status"], "activity_source_resolved")
        self.assertEqual(
            granularity["stage_activity_counts"],
            [{"stage_id": "camera_boot", "activity_count": 2}],
        )
        self.assertEqual(granularity["review_candidate_count"], 0)
        self.assertEqual(result["compiler"]["granularity_observation"], granularity)
        self.assertNotIn("actions", artifact["scenario_json"])
        self.assertEqual(set(artifact["scenario_json"]), {
            "version", "scenario", "description", "source_mermaid", "constants",
            "process_groups", "stage_search_window", "stages", "relations", "relation_defaults",
            "nodes", "node_relations",
        })
        nodes = {item["id"]: item for item in artifact["scenario_json"]["nodes"]}
        self.assertEqual(nodes["action_open_camera"]["type"], "action")
        self.assertIsNone(nodes["action_open_camera"]["parent_id"])
        self.assertEqual(nodes["phase_start_session"]["parent_id"], "action_open_camera")
        self.assertEqual(nodes["camera_boot"]["parent_id"], "phase_start_session")
        self.assertEqual(nodes["start_camera"]["parent_id"], "camera_boot")
        self.assertEqual(nodes["configure_session"]["type"], "activity")
        self.assertIsNone(nodes["configure_session"]["start"])
        self.assertEqual(
            artifact["scenario_json"]["node_relations"][0]["type"],
            "sequence",
        )

    def test_reports_multi_implementation_activity_as_non_blocking_review_candidate(self) -> None:
        discovery = self.save_discovery()["artifact"]
        mermaid = """%% @scenario camera_startup version=2.0
%% @description 用户打开相机到预览首帧可见。
%% @process_group application=camera_app
%% @action {"id":"action_open_camera","order":1,"name":"打开相机","description":"用户进入相机并等待预览"}
%% @phase {"id":"phase_start_session","action_id":"action_open_camera","order":1,"name":"启动相机会话","description":"创建并配置预览会话"}
%% @stage camera_boot order=1 process=camera_app required name="启动并配置相机"
%%   matcher: unavailable
%% @stage_hierarchy {"stage_id":"camera_boot","action_id":"action_open_camera","phase_id":"phase_start_session"}
%% @activity {"id":"start_and_configure","stage_id":"camera_boot","order":1,"name":"启动并配置","description":"启动入口调用会话配置"}
%% @implementation {"stage_id":"camera_boot","activity_id":"start_and_configure","order":1,"symbol":"StartCamera","path":"src/camera_flow.cpp","line":1,"relation":"entry"}
%% @implementation {"stage_id":"camera_boot","activity_id":"start_and_configure","order":2,"symbol":"ConfigureSession","path":"src/camera_flow.cpp","line":7,"relation":"call"}
flowchart LR
    S1["启动并配置相机"]
"""

        result = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=mermaid,
        )

        granularity = result["compiler"]["granularity_observation"]
        self.assertEqual(granularity["structural_status"], "activity_graph_with_aggregates")
        self.assertEqual(granularity["review_candidate_count"], 1)
        self.assertEqual(
            granularity["activities_with_multiple_implementations"],
            [{"activity_id": "start_and_configure", "stage_id": "camera_boot", "implementation_count": 2}],
        )

    def test_hierarchical_stage_can_preserve_an_explicit_source_gap(self) -> None:
        discovery = self.save_discovery()["artifact"]
        mermaid = """%% @scenario camera_startup version=2.0
%% @description 用户打开相机到预览首帧可见。
%% @process_group application=camera_app
%% @action {"id":"action_open_camera","order":1,"name":"打开相机","description":"用户进入相机并等待预览"}
%% @phase {"id":"phase_external_prepare","action_id":"action_open_camera","order":1,"name":"外部准备","description":"当前代码范围未包含具体实现"}
%% @stage external_prepare order=1 process=camera_app required name="准备外部图像服务"
%%   matcher: unavailable
%% @stage_hierarchy {"stage_id":"external_prepare","action_id":"action_open_camera","phase_id":"phase_external_prepare"}
flowchart LR
    S1["准备外部图像服务"]
"""

        artifact = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=mermaid,
        )["artifact"]

        hierarchy = artifact["stage_hierarchy"]
        self.assertEqual(hierarchy["quality"]["status"], "partial_source_binding")
        self.assertEqual(hierarchy["unresolved_stage_ids"], ["external_prepare"])
        self.assertEqual(hierarchy["implementations"], [])
        self.assertIn("尚未关联到当前代码仓范围", "\n".join(artifact["unknowns"]))

    def test_scenario_revision_preserves_parent_lineage_and_old_version(self) -> None:
        discovery = self.save_discovery()["artifact"]
        mermaid = """%% @scenario camera_startup version=2.0
%% @description 用户打开相机到预览首帧可见。
%% @process_group application=camera_app
%% @stage camera_boot order=1 process=camera_app required name="启动并等待预览"
%%   start: trace_point "CameraStart" exact in application
%%   end: trace_point "PreviewReady" exact in application
flowchart LR
    S1[启动并等待预览]
"""
        first = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=mermaid,
        )["artifact"]
        second = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=mermaid,
            parent_scenario_artifact_id=first["artifact_id"],
            revision_request="补充阶段说明。",
        )["artifact"]
        branch = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=mermaid,
            parent_scenario_artifact_id=first["artifact_id"],
            revision_request="从第一版尝试另一种拆分。",
        )["artifact"]

        self.assertNotEqual(second["artifact_id"], first["artifact_id"])
        self.assertEqual(second["revision"], first["revision"] + 1)
        self.assertEqual(second["parent_scenario_artifact_id"], first["artifact_id"])
        self.assertEqual(second["revision_request"], "补充阶段说明。")
        self.assertEqual(branch["revision"], second["revision"] + 1)
        self.assertEqual(branch["parent_scenario_artifact_id"], first["artifact_id"])
        self.assertEqual(self.manager.read_scenario(first["artifact_id"])["artifact_id"], first["artifact_id"])

    def test_new_root_analysis_does_not_reuse_prior_versions_or_parentage(self) -> None:
        first_discovery = self.manager.save_discovery(
            "local_camera_test",
            self.discovery(),
            subpath="src",
            application_flow_analysis_id="application-flow:root-a",
        )["artifact"]
        second_discovery = self.manager.save_discovery(
            "local_camera_test",
            self.discovery(),
            subpath="src",
            application_flow_analysis_id="application-flow:root-b",
        )["artifact"]

        first_scenario = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=first_discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=self.compilable_mermaid(),
        )["artifact"]
        second_scenario = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=second_discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=self.compilable_mermaid(),
        )["artifact"]
        revised_second = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=second_discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=self.compilable_mermaid(),
            parent_scenario_artifact_id=second_scenario["artifact_id"],
            revision_request="继续修改第二条分析。",
        )["artifact"]

        self.assertEqual(first_scenario["revision"], 1)
        self.assertEqual(second_scenario["revision"], 1)
        self.assertEqual(revised_second["revision"], 2)
        self.assertEqual(revised_second["parent_scenario_artifact_id"], second_scenario["artifact_id"])
        isolated_catalog = self.manager.list_assets(application_flow_analysis_id="application-flow:root-b")
        self.assertEqual(
            [item["artifact_id"] for item in isolated_catalog["discoveries"]],
            [second_discovery["artifact_id"]],
        )
        self.assertEqual(
            {item["artifact_id"] for item in isolated_catalog["scenarios"]},
            {second_scenario["artifact_id"], revised_second["artifact_id"]},
        )

        with self.assertRaises(PlatformError) as cross_read:
            self.manager.require_analysis_scope(
                "application_flow_scenario",
                first_scenario["artifact_id"],
                "application-flow:root-b",
            )
        self.assertEqual(cross_read.exception.code, "APPLICATION_FLOW_ANALYSIS_SCOPE_MISMATCH")

        with self.assertRaises(PlatformError) as cross_analysis:
            self.manager.compile_mermaid(
                "local_camera_test",
                discovery_artifact_id=second_discovery["artifact_id"],
                flow_id="camera_startup",
                mermaid=self.compilable_mermaid(),
                parent_scenario_artifact_id=first_scenario["artifact_id"],
                revision_request="错误地引用第一条分析。",
            )
        self.assertEqual(cross_analysis.exception.code, "APPLICATION_FLOW_ANALYSIS_SCOPE_MISMATCH")

    def test_flow_assets_support_leaf_and_cascade_logical_deletion(self) -> None:
        discovery = self.save_discovery()["artifact"]
        scenario = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid="""%% @scenario camera_startup version=2.0
%% @process_group application=camera_app
%% @stage camera_boot order=1 process=camera_app required name="启动并等待预览"
%%   start: trace_point "CameraStart" exact in application
%%   end: trace_point "PreviewReady" exact in application
flowchart LR
    S1[启动并等待预览]
""",
        )["artifact"]

        with self.assertRaises(PlatformError) as child_error:
            self.manager.delete_asset("application_flow_discovery", discovery["artifact_id"])
        self.assertEqual(child_error.exception.code, "APPLICATION_FLOW_ASSET_HAS_CHILDREN")

        deleted = self.manager.delete_asset(
            "application_flow_discovery",
            discovery["artifact_id"],
            cascade=True,
        )
        self.assertEqual(deleted["cascade_count"], 1)
        catalog = self.manager.list_assets()
        self.assertFalse(any(item["artifact_id"] == discovery["artifact_id"] for item in catalog["discoveries"]))
        self.assertFalse(any(item["artifact_id"] == scenario["artifact_id"] for item in catalog["scenarios"]))
        with self.assertRaises(PlatformError) as discovery_error:
            self.manager.read_discovery(discovery["artifact_id"])
        self.assertEqual(discovery_error.exception.code, "APPLICATION_FLOW_ASSET_NOT_FOUND")
        self.assertEqual(
            self.store.read("application_flow_scenario", scenario["artifact_id"])["artifact_id"],
            scenario["artifact_id"],
        )

    def test_mermaid_compiler_uses_draft_matcher_without_inventing_trace_events(self) -> None:
        discovery = self.save_discovery()["artifact"]
        mermaid = """%% @scenario camera_startup version=2.0
%% @stage camera_boot order=1 process=camera_app required name="启动并等待预览"
%%   matcher: unavailable
flowchart TD
    S1[启动并等待预览]
"""

        result = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid=mermaid,
        )

        artifact = result["artifact"]
        self.assertEqual(artifact["scenario_json"]["description"], self.discovery()["flows"][0]["description"])
        self.assertEqual(artifact["scenario_json"]["process_groups"], {"application": ["camera_app"]})
        self.assertEqual(artifact["scenario_json"]["stages"][0]["matcher"]["type"], "composition")
        self.assertEqual(artifact["quality"]["executable_status"], "draft_needs_trace_evidence")
        self.assertEqual(artifact["stage_evidence"][0]["boundary_anchors"], [])
        self.assertTrue(any("camera_boot" in item for item in artifact["unknowns"]))

    def test_stage_three_enriches_verified_source_anchors_and_recompiles_assets(self) -> None:
        discovery_value = self.discovery()
        discovery_value["flows"][0]["trace_anchor_candidates"] = []
        discovery = self.manager.save_discovery(
            "local_camera_test",
            discovery_value,
            subpath="src",
        )["artifact"]
        draft = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid="""%% @scenario camera_startup version=2.0
%% @process_group application=camera_app
%% @action {"id":"action_open_camera","order":1,"name":"打开相机","description":"用户进入相机并等待预览"}
%% @phase {"id":"phase_start_session","action_id":"action_open_camera","order":1,"name":"启动相机会话","description":"建立预览链路"}
%% @stage camera_boot order=1 process=camera_app required name="启动并等待预览"
%%   matcher: unavailable
%% @stage_hierarchy {"stage_id":"camera_boot","action_id":"action_open_camera","phase_id":"phase_start_session"}
%% @implementation {"stage_id":"camera_boot","order":1,"symbol":"StartCamera","path":"src/camera_flow.cpp","line":1,"relation":"entry"}
flowchart LR
    S1[启动并等待预览]
""",
        )["artifact"]

        result = self.manager.enrich_trace_anchors(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            scenario_artifact_id=draft["artifact_id"],
            flow_id="camera_startup",
            stage_results=[{
                "stage_id": "camera_boot",
                "start": {
                    "event_name": "CameraStart",
                    "path": "src/camera_flow.cpp",
                    "line": 3,
                    "macro": "HITRACE_METER_NAME",
                    "symbol": "StartCamera",
                },
                "end": {
                    "event_name": "PreviewReady",
                    "path": "src/camera_flow.cpp",
                    "line": 14,
                    "macro": "HITRACE_METER_NAME",
                    "symbol": "OnPreviewReady",
                },
                "notes": "入口与回调各有一个源码字面量事件。",
            }],
        )

        artifact = result["artifact"]
        self.assertNotEqual(result["discovery_artifact"]["artifact_id"], discovery["artifact_id"])
        self.assertEqual(result["enrichment"]["complete_stage_count"], 1)
        self.assertEqual(result["enrichment"]["found_boundary_count"], 2)
        self.assertEqual(artifact["quality"]["executable_status"], "ready")
        self.assertEqual(artifact["quality"]["anchor_search_status"], "complete")
        self.assertEqual(artifact["boundary_analysis"][0]["status"], "complete")
        self.assertIn('start: trace_point "CameraStart"', artifact["mermaid"])
        self.assertIn('end: trace_point "PreviewReady"', artifact["mermaid"])
        self.assertEqual(artifact["stage_hierarchy"]["representation"], "hierarchical_authoring_projection")
        self.assertEqual(artifact["stage_hierarchy"]["implementations"][0]["symbol"], "StartCamera")
        self.assertEqual(
            {item["boundary"] for item in artifact["stage_evidence"][0]["boundary_anchors"]},
            {"start", "end"},
        )

    def test_stage_three_normalizes_pretty_function_wrapper_to_trace_selector(self) -> None:
        self.write_function_derived_trace_fixture()
        discovery_value = self.discovery()
        discovery_value["flows"][0]["trace_anchor_candidates"] = []
        discovery = self.manager.save_discovery(
            "local_camera_test",
            discovery_value,
            subpath="src",
        )["artifact"]
        draft = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid="""%% @scenario camera_startup version=2.0
%% @process_group application=camera_app
%% @stage camera_boot order=1 process=camera_app required name="启动并等待预览"
%%   matcher: unavailable
flowchart LR
    S1[启动并等待预览]
""",
        )["artifact"]
        submitted_declaration = (
            "int32_t HCameraService::CreateCaptureSession("
            "sptr<ICaptureSession>& session, int32_t opMode)"
        )

        result = self.manager.enrich_trace_anchors(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            scenario_artifact_id=draft["artifact_id"],
            flow_id="camera_startup",
            stage_results=[{
                "stage_id": "camera_boot",
                "start": {
                    "event_name": submitted_declaration,
                    "path": "src/hcamera_service.cpp",
                    "line": 5,
                    "macro": "CAMERA_SYNC_TRACE",
                    "symbol": submitted_declaration,
                    "event_label_match": "wrapper_literal",
                },
                "end": {
                    "event_name": submitted_declaration,
                    "path": "src/hcamera_service.cpp",
                    "line": 5,
                    "macro": "CAMERA_SYNC_TRACE",
                    "symbol": submitted_declaration,
                    "event_label_match": "wrapper_literal",
                },
                "notes": "同一 RAII 作用域覆盖该 stage。",
            }],
        )

        artifact = result["artifact"]
        stage = artifact["scenario_json"]["stages"][0]
        self.assertEqual(stage["start"]["name"], "HCameraService::CreateCaptureSession")
        self.assertEqual(stage["end"]["name"], "HCameraService::CreateCaptureSession")
        self.assertEqual(stage["start"]["name_match"], "substring_case_insensitive")
        boundary = artifact["boundary_analysis"][0]["start"]
        self.assertEqual(boundary["instrumentation"]["event_label_match"], "function_derived")

    def test_stage_three_preserves_partial_findings_without_promoting_matcher(self) -> None:
        discovery_value = self.discovery()
        discovery_value["flows"][0]["trace_anchor_candidates"] = []
        discovery = self.manager.save_discovery(
            "local_camera_test",
            discovery_value,
            subpath="src",
        )["artifact"]
        draft = self.manager.compile_mermaid(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            mermaid="""%% @scenario camera_startup version=2.0
%% @stage camera_boot order=1 process=camera_app required name="启动并等待预览"
%%   matcher: unavailable
flowchart LR
    S1[启动并等待预览]
""",
        )["artifact"]

        result = self.manager.enrich_trace_anchors(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            scenario_artifact_id=draft["artifact_id"],
            flow_id="camera_startup",
            stage_results=[{
                "stage_id": "camera_boot",
                "start": {
                    "event_name": "CameraStart",
                    "path": "src/camera_flow.cpp",
                    "line": 3,
                    "macro": "HITRACE_METER_NAME",
                    "symbol": "StartCamera",
                },
                "notes": "完成边界未找到源码插桩。",
            }],
        )

        artifact = result["artifact"]
        self.assertEqual(artifact["quality"]["executable_status"], "draft_needs_trace_evidence")
        self.assertEqual(artifact["quality"]["anchor_search_status"], "partial")
        self.assertEqual(artifact["boundary_analysis"][0]["status"], "partial")
        self.assertEqual(artifact["boundary_analysis"][0]["end"]["status"], "unavailable")
        self.assertIn("matcher: unavailable", artifact["mermaid"])
        self.assertNotIn("start: trace_point", artifact["mermaid"])

    def test_mermaid_compiler_rejects_incomplete_stage_metadata(self) -> None:
        discovery = self.save_discovery()["artifact"]
        mermaid = self.compilable_mermaid().replace(
            '%%   end: trace_point "PreviewReady" exact in application\n',
            "",
        )

        with self.assertRaises(PlatformError) as raised:
            self.manager.compile_mermaid(
                "local_camera_test",
                discovery_artifact_id=discovery["artifact_id"],
                flow_id="camera_startup",
                mermaid=mermaid,
            )

        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_MERMAID_METADATA_INVALID")

    def test_scenario_contract_accepts_unrelated_domain_values(self) -> None:
        scenario = self.scenario()
        scenario.update(
            {
                "scenario": "document_export",
                "description": "文档导出请求进入后台任务并发布结果。",
                "source_mermaid": "document_export.mermaid",
                "constants": {"EXPORT_TIMEOUT_NS": 3000000000},
                "process_groups": {"workers": ["document_worker"]},
            }
        )
        scenario["stages"] = [
            {
                "id": "export_job",
                "name": "执行文档导出",
                "process_name": "document_worker",
                "order": 1,
                "optional": False,
                "start": {
                    "type": "trace_point",
                    "name": "ExportRequested",
                    "name_match": "exact",
                    "which": "start",
                    "process": {"process_group": "workers"},
                },
                "end": {
                    "type": "trace_point",
                    "name": "ExportPublished",
                    "name_match": "exact",
                    "which": "end",
                    "process": {"process_group": "workers"},
                },
            }
        ]
        mermaid = """%% @scenario document_export version=2.0
%% @stage export_job order=1 process=document_worker required name="执行文档导出"
flowchart LR
    S1[执行文档导出]
"""

        validated = self.manager.validate_scenario(scenario, mermaid)

        self.assertEqual(validated["scenario"], "document_export")
        self.assertEqual(validated["stages"][0]["id"], "export_job")

    def test_scoped_source_event_can_bind_both_stage_boundaries(self) -> None:
        discovery = self.save_discovery()["artifact"]
        scenario = self.scenario()
        scenario["stages"][0]["end"]["name"] = "CameraStart"

        saved = self.manager.save_scenario(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            scenario_json=scenario,
            mermaid=self.mermaid(),
            stage_evidence=[
                {
                    "stage_id": "camera_boot",
                    "source_evidence_ids": ["ev_start_camera"],
                    "trace_anchor_status": "observed_in_source",
                    "notes": "作用域型插桩使用同一 event 的 start/end。",
                    "boundary_anchors": [
                        {"boundary": "start", "anchor_id": "anchor_camera_start"},
                        {"boundary": "end", "anchor_id": "anchor_camera_start"},
                    ],
                }
            ],
            unknowns=["仍需真实 Trace 确认运行时发生。"],
        )

        links = saved["artifact"]["stage_evidence"][0]["boundary_anchors"]
        self.assertEqual([item["anchor_id"] for item in links], ["anchor_camera_start", "anchor_camera_start"])

    def test_model_facing_assets_do_not_name_the_reference_workload_anchor(self) -> None:
        model_facing_files = [
            self.skill_root / "SKILL.md",
            self.skill_root / "agents" / "openai.yaml",
            *sorted((self.skill_root / "assets").glob("*")),
            *sorted((self.skill_root / "references").glob("*")),
        ]

        for path in model_facing_files:
            with self.subTest(path=path.name):
                self.assertNotIn("runv3", path.read_text(encoding="utf-8").lower())

    def test_skill_directly_reconstructs_complete_operation_with_autonomous_tool_choice(self) -> None:
        skill = (self.skill_root / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("Agent 负责判断合理的操作边界", skill)
        self.assertIn("页面不再设置候选确认步骤", skill)
        self.assertIn("一个完整操作", skill)
        self.assertIn("用户动作 → 业务大阶段 → 可定位子阶段 → 细粒度活动关系 → 函数实现", skill)
        self.assertIn("@stage_hierarchy", skill)
        self.assertIn("@activity", skill)
        self.assertIn("@activity_relation", skill)
        self.assertIn("@implementation", skill)
        self.assertIn("application-flow-scenario/v2", skill)
        self.assertIn("暂时无法落到当前源码范围", skill)
        self.assertIn("原生源码搜索", skill)

    def test_unverified_required_stage_remains_draft(self) -> None:
        discovery = self.save_discovery()["artifact"]
        saved = self.manager.save_scenario(
            "local_camera_test",
            discovery_artifact_id=discovery["artifact_id"],
            flow_id="camera_startup",
            scenario_json=self.scenario(),
            mermaid=self.mermaid(),
            stage_evidence=[
                {
                    "stage_id": "camera_boot",
                    "source_evidence_ids": ["ev_start_camera", "ev_preview_ready"],
                    "trace_anchor_status": "inferred_needs_trace",
                    "notes": "代码链清楚，但本场景尚未采集 Trace。",
                }
            ],
            unknowns=["需要真实 Trace。"],
        )

        self.assertEqual(saved["artifact"]["quality"]["executable_status"], "draft_needs_trace_evidence")

    def test_stage_boundary_must_reference_the_matching_source_anchor(self) -> None:
        discovery = self.save_discovery()["artifact"]
        scenario = self.scenario()
        scenario["stages"][0]["start"]["name"] = "DifferentStartEvent"

        with self.assertRaises(PlatformError) as raised:
            self.manager.save_scenario(
                "local_camera_test",
                discovery_artifact_id=discovery["artifact_id"],
                flow_id="camera_startup",
                scenario_json=scenario,
                mermaid=self.mermaid(),
                stage_evidence=[
                    {
                        "stage_id": "camera_boot",
                        "source_evidence_ids": ["ev_start_camera", "ev_preview_ready"],
                        "trace_anchor_status": "observed_in_source",
                        "notes": "边界名称与源码锚点应保持一致。",
                        "boundary_anchors": [
                            {"boundary": "start", "anchor_id": "anchor_camera_start"},
                            {"boundary": "end", "anchor_id": "anchor_preview_ready"},
                        ],
                    }
                ],
                unknowns=[],
            )

        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_STAGE_EVIDENCE_INVALID")

    def test_mermaid_stage_metadata_must_match_json(self) -> None:
        mismatched = self.mermaid().replace("order=1", "order=2")
        with self.assertRaises(PlatformError) as raised:
            self.manager.validate_scenario(self.scenario(), mismatched)
        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_MERMAID_INVALID")

    def test_stage_process_must_be_declared_by_process_groups(self) -> None:
        scenario = self.scenario()
        scenario["stages"][0]["process_name"] = "undeclared_process"
        with self.assertRaises(PlatformError) as raised:
            self.manager.validate_scenario(scenario, self.mermaid())
        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_SCENARIO_INVALID")

    def test_snapshot_change_invalidates_old_discovery(self) -> None:
        discovery = self.save_discovery()["artifact"]
        changed = copy.deepcopy(self.connector)
        changed["snapshot"]["snapshot_id"] = "source_snapshot_camera_v2"
        self.connectors.registry.write({"connectors": [changed]})

        with self.assertRaises(PlatformError) as raised:
            self.manager.save_scenario(
                "local_camera_test",
                discovery_artifact_id=discovery["artifact_id"],
                flow_id="camera_startup",
                scenario_json=self.scenario(),
                mermaid=self.mermaid(),
                stage_evidence=[],
                unknowns=[],
            )
        self.assertEqual(raised.exception.code, "APPLICATION_FLOW_SOURCE_BINDING_MISMATCH")

    def test_source_read_cannot_escape_registered_repository(self) -> None:
        outside = self.root / "outside.cpp"
        outside.write_text("void Outside() {}\n", encoding="utf-8")
        with self.assertRaises(PlatformError) as raised:
            self.manager.read_source("local_camera_test", path="../outside.cpp")
        self.assertEqual(raised.exception.code, "SOURCE_FILE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
