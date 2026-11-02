from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as platform_app
from backend.core import ArtifactStore, sha256_file
from backend.skills import registered_action


class RealTraceReasoningTests(unittest.TestCase):
    def test_agent_can_save_explicitly_unbound_hotspot_code(self) -> None:
        action = registered_action("reasoning.paste_hotspot_code")

        self.assertEqual(action["risk_level"], "write_local")
        self.assertEqual(action["input_schema"]["required"], ["code"])
        self.assertEqual(action["input_schema"]["properties"]["code"]["maxLength"], 16384)

    def test_agent_can_select_a_source_observed_candidate_without_claiming_sampled_hotness(self) -> None:
        action = registered_action("reasoning.select_hotspot_source_candidate")

        self.assertIn("source_resolution_artifact_id", action["input_schema"]["required"])
        self.assertIn("callee_function", action["input_schema"]["properties"]["candidate_kind"]["enum"])
        self.assertIn("without CPU samples", action["implementation_ref"])

    def test_agent_input_intent_actions_use_browser_authorized_references(self) -> None:
        text_action = registered_action("assistant.use_pending_text_as_hotspot")
        trace_action = registered_action("assistant.import_pending_trace")

        self.assertEqual(text_action["input_schema"]["required"], ["input_id", "content_mode"])
        self.assertEqual(
            text_action["input_schema"]["properties"]["content_mode"]["enum"],
            ["entire_input", "first_code_fence", "line_range"],
        )
        self.assertEqual(text_action["input_schema"]["properties"]["line_start"]["minimum"], 1)
        self.assertEqual(trace_action["input_schema"]["required"], ["attachment_id"])
        self.assertIn("attachment_", trace_action["input_schema"]["properties"]["attachment_id"]["pattern"])

    def test_artifact_store_lists_newest_trace_artifacts_for_one_connector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="test-project")
            arguments = {
                "source_binding": {"match_level": "unknown"},
                "producer": {"name": "test", "version": "1"},
            }
            first = store.write("trace_analysis_result", {"input_kind": "raw_htrace"}, source_connector_id="first", **arguments)
            second = store.write("trace_analysis_result", {"input_kind": "raw_htrace"}, source_connector_id="second", **arguments)
            self.assertEqual(store.list_artifacts("trace_analysis_result", source_connector_id="first"), [first])
            self.assertEqual(store.list_artifacts("trace_analysis_result", source_connector_id="second"), [second])

    def test_external_report_catalog_does_not_invent_source_locations(self) -> None:
        catalog = platform_app._external_reasoning_hotspot_catalog(
            {
                "hotspot_candidates": [
                    {
                        "hotspot_id": "hs-1",
                        "function_ref": {"symbol": "RenderFrame", "module": "ARKUI"},
                        "active_time_metrics": {"total_duration_ms": 12.4},
                    }
                ]
            },
            limit=10,
        )
        event = catalog["events"][0]
        self.assertEqual(event["event_name"], "RenderFrame")
        self.assertIsNone(event["source_location"])
        self.assertEqual(event["source_resolution"], "external_symbol_only")

    def test_external_report_catalog_groups_top_events_per_module(self) -> None:
        catalog = platform_app._external_reasoning_hotspot_catalog(
            {
                "hotspot_candidates": [
                    {"hotspot_id": "ark-1", "function_ref": {"symbol": "Layout", "module": "ArkUI"}, "active_time_metrics": {"total_duration_ms": 10.0}},
                    {"hotspot_id": "ark-2", "function_ref": {"symbol": "Paint", "module": "ArkUI"}, "active_time_metrics": {"total_duration_ms": 5.0}},
                    {"hotspot_id": "media-1", "function_ref": {"symbol": "Decode", "module": "Media"}, "active_time_metrics": {"total_duration_ms": 7.0}},
                ]
            },
            limit=100,
            group_by_module=True,
            per_module_limit=1,
        )

        self.assertEqual([group["module_name"] for group in catalog["module_event_groups"]], ["ArkUI", "Media"])
        self.assertEqual([event["event_id"] for event in catalog["events"]], ["ark-1", "media-1"])
        self.assertEqual([event["rank"] for event in catalog["events"]], [1, 2])
        self.assertEqual(catalog["module_event_groups"][0]["event_group_count"], 2)

    def test_structural_stage_report_reaches_the_page_hotspot_catalog(self) -> None:
        analysis = platform_app.normalize_external_report(
            {
                "arbitraryWrapper": {
                    "anotherWrapper": {
                        "stages": [
                            {
                                "id": "opaque-stage-value",
                                "name": "opaque-stage-label",
                                "top_events": [
                                    {
                                        "name": "opaque-trace-label",
                                        "dur_ms": 2.75,
                                        "process_name": "opaque-module-label",
                                    }
                                ],
                            }
                        ]
                    }
                }
            },
            trace_name="structural-report.json",
            trace_sha256="4" * 64,
        )
        catalog = platform_app._external_reasoning_hotspot_catalog(analysis, limit=10)

        self.assertEqual(catalog["quality"]["status"], "available")
        self.assertEqual(len(catalog["events"]), 1)
        self.assertEqual(catalog["events"][0]["event_name"], "opaque-trace-label")
        self.assertEqual(catalog["events"][0]["module"], "opaque-module-label")

    def test_external_stage_catalog_returns_every_stage_and_bypasses_module_top_n(self) -> None:
        analysis = platform_app.normalize_external_report(
            {
                "stages": [
                    {
                        "id": "prepare",
                        "name": "Prepare",
                        "order": 1,
                        "start_ns": 1_000_000,
                        "end_ns": 5_000_000,
                        "duration_ms": 4.0,
                        "event_count": 3,
                        "total_event_duration_ms": 3.0,
                        "top_events": [
                            {"name": "event-a", "ts_ms": 1.1, "dur_ms": 1.0, "process_name": "module-a"},
                            {"name": "event-b", "ts_ms": 2.1, "dur_ms": 1.0, "process_name": "module-a"},
                            {"name": "event-c", "ts_ms": 3.1, "dur_ms": 1.0, "process_name": "module-a"},
                        ],
                    },
                    {
                        "id": "finish",
                        "name": "Finish",
                        "order": 2,
                        "start_ns": 5_000_000,
                        "end_ns": 7_000_000,
                        "duration_ms": 2.0,
                        "event_count": 0,
                        "top_events": [],
                    },
                ]
            },
            trace_name="stage-groups.json",
            trace_sha256="9" * 64,
        )

        catalog = platform_app._external_reasoning_hotspot_catalog(
            analysis,
            limit=100,
            group_by_module=True,
            per_module_limit=1,
        )

        self.assertEqual(len(catalog["events"]), 1)
        self.assertEqual([stage["stage_id"] for stage in catalog["stage_event_groups"]], ["prepare", "finish"])
        self.assertEqual([event["event_name"] for event in catalog["stage_event_groups"][0]["events"]], ["event-a", "event-b", "event-c"])
        self.assertEqual(catalog["stage_event_groups"][0]["returned_event_count"], 3)
        self.assertEqual(catalog["stage_event_groups"][0]["reported_event_count"], 3)
        self.assertEqual(catalog["stage_event_groups"][1]["events"], [])

    def test_catalog_event_lookup_reaches_events_after_flat_limit(self) -> None:
        late_event = {"event_id": "late-stage-event"}
        catalog = {
            "events": [{"event_id": "flat-event"}],
            "stage_event_groups": [{"stage_id": "late", "events": [late_event]}],
        }

        self.assertIs(platform_app._catalog_event_by_id(catalog, "late-stage-event"), late_event)

    def test_external_stage_catalog_treats_boundary_fallback_as_non_blocking_anchor(self) -> None:
        catalog = platform_app._external_reasoning_hotspot_catalog(
            {
                "quality": {
                    "normalization": {
                        "contract": "stage-flow-compatible/v2",
                        "input_stage_count": 1,
                        "boundary_anchor_fallback_count": 1,
                    }
                },
                "hotspot_candidates": [
                    {
                        "hotspot_id": "anchor-1",
                        "candidate_role": "trace_anchor_only",
                        "function_ref": {"symbol": "LayoutTask::Run", "module": "ArkUI"},
                        "active_time_metrics": {"total_duration_ms": 0.0, "call_count": 1},
                        "measurement_semantics": {
                            "duration_kind": "unmeasured_trace_anchor",
                            "cpu_self_time_available": False,
                            "callee_attribution_available": False,
                        },
                    }
                ],
            },
            limit=10,
        )

        self.assertEqual(catalog["quality"]["status"], "degraded")
        self.assertEqual(catalog["quality"]["reason"], "")
        self.assertIn("不是 CPU self-time", catalog["quality"]["note"])
        self.assertEqual(catalog["events"][0]["candidate_role"], "trace_anchor_only")
        self.assertIn("start/end Trace 锚点", catalog["events"][0]["summary"])

    def test_external_stage_catalog_reports_schema_diagnostics_only_when_no_event_is_available(self) -> None:
        catalog = platform_app._external_reasoning_hotspot_catalog(
            {
                "quality": {
                    "normalization": {
                        "contract": "stage-flow-compatible/v2",
                        "input_stage_count": 3,
                    }
                },
                "hotspot_candidates": [],
            },
            limit=10,
        )

        self.assertEqual(catalog["quality"]["status"], "unavailable")
        self.assertIn("已识别 3 个阶段", catalog["quality"]["reason"])
        self.assertNotIn("Trace-team records are preserved", catalog["quality"]["reason"])

    def test_reasoning_hotspot_endpoint_forwards_module_grouping_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            analysis = store.write(
                "trace_analysis_result",
                {"input_kind": "raw_htrace", "raw_htrace": {}},
                source_connector_id="connector-test",
                source_binding={"match_level": "unknown"},
                producer={"name": "test", "version": "1"},
            )
            catalog = {
                "events": [],
                "module_event_groups": [],
                "module_catalog": {"groups": []},
                "quality": {"status": "unavailable"},
            }
            with patch.object(platform_app, "store", store), patch.object(platform_app, "_reasoning_hotspot_catalog", return_value=(catalog, {"kind": "test"})) as extractor, patch.object(platform_app, "_write_trace_event_catalog", return_value={"artifact_id": "art_catalog"}):
                response = platform_app.list_reasoning_trace_hotspots(
                    "local-default",
                    platform_app.ReasoningTraceHotspotRequest(
                        analysis_artifact_id=analysis["artifact_id"],
                        group_by_module=True,
                        per_module_limit=4,
                    ),
                )

        self.assertEqual(response["artifact"]["artifact_id"], "art_catalog")
        self.assertTrue(extractor.call_args.kwargs["group_by_module"])
        self.assertEqual(extractor.call_args.kwargs["per_module_limit"], 4)

    def test_historical_workbench_restore_reads_only_its_immutable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            arguments = {
                "source_connector_id": "connector-test",
                "source_binding": {"match_level": "exact"},
                "producer": {"name": "test", "version": "1"},
            }
            trace = store.write(
                "trace_analysis_result",
                {"input_kind": "raw_htrace", "analysis_profile": "restore-test", "quality": {"status": "available"}},
                **arguments,
            )
            code = store.write(
                "hotspot_code_unit",
                {"code": "void Work() {}", "hotspot": {"event_id": "event-1", "event_name": "Work"}},
                **arguments,
            )
            context = store.write(
                "reasoning_context_pack",
                {"strategy_id": "direct_baseline", "context_sha256": "c" * 64, "input_artifacts": [f"artifact:{code['artifact_id']}"]},
                **arguments,
            )
            result = store.write(
                "opportunity_reasoning_result",
                {"strategy_id": "direct_baseline", "context_artifact_id": context["artifact_id"], "result": {"summary": "已保存"}},
                **arguments,
            )
            with patch.object(platform_app, "store", store):
                restored_trace = platform_app.get_reasoning_trace_analysis_result("local-default", trace["artifact_id"])["trace"]
                restored_code = platform_app.get_hotspot_code_unit("local-default", code["artifact_id"])["artifact"]
                restored_context = platform_app.get_reasoning_context("local-default", context["artifact_id"])["artifact"]
                restored_result = platform_app.get_reasoning_result("local-default", result["artifact_id"])["artifact"]

        self.assertEqual(restored_trace["artifact_id"], trace["artifact_id"])
        self.assertEqual(restored_code["artifact_id"], code["artifact_id"])
        self.assertEqual(restored_context["artifact_id"], context["artifact_id"])
        self.assertEqual(restored_result["context_artifact_id"], context["artifact_id"])

    def test_real_source_resolution_extracts_only_the_verified_function_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "worker.cpp"
            source.write_text(
                "void Work()\n{\n  int value = 0;\n  value++;\n}\n\nvoid Other() {}\n",
                encoding="utf-8",
            )
            resolution = {
                "artifact_id": "art_" + "1" * 24,
                "analysis_artifact_id": "art_" + "2" * 24,
                "source_connector_id": "connector-test",
                "source_binding": {"match_level": "exact"},
                "resolution": {"state": "instrumentation_point_exact"},
                "event": {"event_id": "event-1", "event_name": "Work", "metrics": {"self_time_estimate_total_duration_ms": 2.5}},
                "code_block": {
                    "source": {"path": "worker.cpp", "line": 3, "sha256": sha256_file(source)},
                    "match_evidence": {
                        "ast_target": {"name": "Work", "declaration_line": 1, "body_start_line": 2, "body_end_line": 5}
                    },
                },
            }
            with patch.object(platform_app.connectors, "resolve_source_path", return_value=source):
                unit = platform_app._hotspot_code_unit_from_resolution(resolution)

            self.assertEqual(unit["source"]["line_start"], 1)
            self.assertEqual(unit["source"]["line_end"], 5)
            self.assertIn("void Work()", unit["code"])
            self.assertNotIn("void Other()", unit["code"])
            self.assertEqual(unit["selection"]["semantic_role"], "trace_anchor_function")
            self.assertEqual(unit["hotspot_attribution"]["status"], "requires_code_path_investigation")

    def test_text_search_candidate_cannot_become_a_real_trace_code_unit(self) -> None:
        resolution = {
            "artifact_id": "art_" + "3" * 24,
            "resolution": {"state": "symbol_text_candidate"},
            "code_block": {"source": {"path": "worker.cpp", "line": 3}},
        }
        with self.assertRaises(platform_app.PlatformError) as raised:
            platform_app._hotspot_code_unit_from_resolution(resolution)
        self.assertEqual(raised.exception.code, "HOTSPOT_SOURCE_EVIDENCE_REQUIRED")

    def test_investigated_callee_is_source_verified_but_hotness_stays_a_hypothesis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "worker.cpp"
            source.write_text(
                "void Core()\n{\n  for (int i = 0; i < 8; ++i) {}\n}\n\nvoid Anchor()\n{\n  Core();\n}\n",
                encoding="utf-8",
            )
            store = ArtifactStore(root / "artifacts", project_id="local-default")
            arguments = {
                "source_connector_id": "connector-test",
                "source_binding": {"match_level": "dirty_worktree", "resolved_snapshot_id": "snapshot-test"},
                "producer": {"name": "test", "version": "1"},
            }
            analysis = store.write("trace_analysis_result", {"input_kind": "external_trace_analysis_report"}, **arguments)
            resolution = store.write(
                "trace_event_source_resolution",
                {
                    "analysis_artifact_id": analysis["artifact_id"],
                    "event": {"event_id": "event-1", "event_name": "Anchor", "metrics": {"max_duration_ms": 12.0}},
                    "code_block": {"source": {"path": "worker.cpp", "line": 8, "sha256": sha256_file(source)}},
                },
                **arguments,
            )
            request = platform_app.HotspotInvestigatedSourceRequest(
                source_resolution_artifact_id=resolution["artifact_id"],
                path="worker.cpp",
                line_start=1,
                line_end=4,
                function_hint="Core",
                candidate_kind="callee_function",
                relation_to_anchor="Anchor contains a direct call to Core.",
                rationale="Core contains the visible loop while Anchor is a thin trace wrapper.",
                expected_source_sha256=sha256_file(source),
                call_path=[platform_app.HotspotCallPathEvidence(caller_symbol="Anchor", callee_symbol="Core", path="worker.cpp", line=8)],
            )
            with (
                patch.object(platform_app, "store", store),
                patch.object(platform_app.connectors, "get", return_value={"source_root": str(root)}),
                patch.object(platform_app.connectors, "resolve_source_path", return_value=source),
                patch.object(platform_app, "_source_binding", return_value={"match_level": "dirty_worktree", "resolved_snapshot_id": "snapshot-current"}),
            ):
                artifact = platform_app.create_hotspot_code_from_investigated_source("local-default", request)["artifact"]

        self.assertEqual(artifact["function_hint"], "Core")
        self.assertEqual(artifact["selection"]["candidate_kind"], "callee_function")
        self.assertEqual(artifact["hotspot_attribution"]["hotness_evidence_level"], "model_hypothesis_without_cpu_sampling")
        self.assertEqual(artifact["selection"]["call_path"][0]["line_text"], "Core();")
        self.assertIn("for (int i", artifact["code"])


if __name__ == "__main__":
    unittest.main()
