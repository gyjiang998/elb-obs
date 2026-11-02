import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from backend.core import ArtifactStore, PlatformError
from backend.desktop_workspace_rpc import _wait_for_code_graph_job
from backend.parallel_modeling.manager import ParallelModelingManager
from backend.tools.desktop_platform_rpc import (
    MAX_RESPONSE_WIRE_BYTES,
    RPC_GZIP_MARKER,
    RPC_STREAM_READY_MARKER,
    RPC_STREAM_RESPONSE_MARKER,
    SCHEMA_VERSION,
    _ParallelModelingManagerCache,
    _fit_summary,
    _fit_view,
    _search_view,
    _serve_stdio,
    dispatch,
    encode_response_line,
    response_envelope,
)


class DesktopPlatformRpcTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"HETERO_STATE_ROOT": str(self.root)}, clear=False)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def register_source_fixture(self, name: str = "Desktop fixture") -> dict:
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        source = Path(fixture.name)
        (source / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Desktop test",
                        "-c", "user.email=desktop@example.invalid", "commit", "-qm", "fixture"], check=True)
        return dispatch({"method": "POST", "path": "/v1/source-connectors/local", "body": {
            "source_root": str(source), "display_name": name,
        }})["value"]["connector"]

    def test_catalog_and_empty_datasets_use_managed_state(self):
        catalog = dispatch({"method": "GET", "path": "/v1/workload-features/catalog"})
        datasets = dispatch({"method": "GET", "path": "/v1/parallel-modeling/datasets"})
        self.assertEqual(catalog["kind"], "json")
        self.assertEqual(catalog["value"]["schema_version"], "workload_features_catalog/v1")
        self.assertEqual(datasets, {"kind": "json", "value": {"datasets": []}})
        modeling = dispatch({"method": "GET", "path": "/v1/parallel-modeling/catalog"})
        builtin_id = modeling["value"]["builtins"][0]["id"]
        imported = dispatch({"method": "POST", "path": "/v1/parallel-modeling/datasets", "body": {"builtin_id": builtin_id}})
        self.assertTrue(imported["value"]["id"].startswith("ds_"))

    def test_stdio_worker_reuses_parallel_manager_across_requests(self):
        def frame(request_id: str, path: str) -> str:
            request = {
                "schema_version": SCHEMA_VERSION,
                "method": "GET",
                "path": path,
            }
            encoded = base64.b64encode(json.dumps(request).encode()).decode()
            return json.dumps({"request_id": request_id, "request_base64": encoded}) + "\n"

        stdin = io.StringIO(
            frame("catalog", "/v1/parallel-modeling/catalog")
            + frame("datasets", "/v1/parallel-modeling/datasets")
        )
        stdout = io.StringIO()
        with (
            patch("backend.tools.desktop_platform_rpc.ParallelModelingManager", wraps=ParallelModelingManager) as constructor,
            patch("sys.stdin", stdin),
            patch("sys.stdout", stdout),
        ):
            self.assertEqual(_serve_stdio(), 0)

        lines = stdout.getvalue().splitlines()
        self.assertTrue(lines[0].startswith(RPC_STREAM_READY_MARKER))
        self.assertTrue(lines[1].startswith(f"{RPC_STREAM_RESPONSE_MARKER}catalog\t"))
        self.assertTrue(lines[2].startswith(f"{RPC_STREAM_RESPONSE_MARKER}datasets\t"))
        self.assertEqual(constructor.call_count, 1)

    def test_parallel_manager_cache_is_root_scoped_bounded_and_disk_fresh(self):
        cache = _ParallelModelingManagerCache(max_entries=1)
        first = cache.get(self.root)
        record = {
            "schema_version": "parallel_modeling_dataset/v1",
            "id": "ds_" + "a" * 24,
            "name": "written after manager construction",
            "created_at": "2026-10-29T00:00:00Z",
        }
        (first.state_root / "datasets" / f'{record["id"]}.json').write_text(
            json.dumps(record), encoding="utf-8"
        )
        listed = response_envelope(
            {"schema_version": SCHEMA_VERSION, "method": "GET", "path": "/v1/parallel-modeling/datasets"},
            manager_cache=cache,
        )
        self.assertEqual(listed["result"]["value"]["datasets"][0]["name"], record["name"])

        with tempfile.TemporaryDirectory() as other_directory:
            other_root = Path(other_directory)
            second = cache.get(other_root)
            self.assertEqual(second.capture_state_root, other_root.resolve())
            self.assertIsNot(second, first)
            # max_entries=1 evicts the first root; asking for it again rebuilds
            # only that root instead of accidentally returning the other state.
            rebuilt = cache.get(self.root)
            self.assertEqual(rebuilt.capture_state_root, self.root.resolve())
            self.assertIsNot(rebuilt, first)

    def test_desktop_workspace_overview_registers_real_source_connector(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "README.md").write_text("desktop source connector\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=Desktop test", "-c", "user.email=desktop@example.invalid", "commit", "-qm", "fixture"], check=True)
            registered = dispatch({"method": "POST", "path": "/v1/source-connectors/local", "body": {
                "source_root": str(source), "display_name": "Product source",
            }})["value"]["connector"]
            overview = dispatch({"method": "GET", "path": "/v1/overview"})["value"]
            self.assertEqual(overview["default_connector"]["connector_id"], registered["connector_id"])
            self.assertEqual(overview["connectors"][0]["source_root"], str(source.resolve()))
            self.assertIn("optimization_kb", overview)
            self.assertIn("hardware_kb", overview)

    def test_desktop_workspace_knowledge_search_and_application_flow_catalog_are_real(self):
        search = dispatch({"method": "POST", "path": "/v1/optimization-kb/search", "body": {
            "query": "matrix", "include_unverified": True, "include_cases": False,
        }})["value"]
        self.assertEqual(search["knowledge_base"], "optimization")
        self.assertTrue(search["kb_version"])
        self.assertGreater(len(search["records"]), 0)
        flows = dispatch({"method": "GET", "path": "/v1/application-flows"})["value"]
        self.assertEqual(flows["schema_version"], "application_flow_asset_catalog/v1")
        self.assertEqual(flows["discoveries"], [])
        self.assertEqual(flows["scenarios"], [])

    def test_desktop_application_flow_agent_action_runs_without_web_service(self):
        connector = self.register_source_fixture("Flow action fixture")
        result = dispatch({"method": "POST", "path": "/v1/application-flows/agent-action", "body": {
            "action_id": "application_flow.inspect_repository",
            "input": {"source_connector_id": connector["connector_id"]},
            "context": {
                "task_kind": "application_flow_generation",
                "application_flow_analysis_id": "analysis_fixture",
                "source_connector_id": connector["connector_id"],
            },
        }})["value"]
        self.assertEqual(result["source_connector_id"], connector["connector_id"])
        self.assertGreaterEqual(result["scan"]["files_considered"], 1)
        with self.assertRaises(PlatformError) as blocked:
            dispatch({"method": "POST", "path": "/v1/application-flows/agent-action", "body": {
                "action_id": "application_flow.run_arbitrary_python", "input": {},
            }})
        self.assertEqual(blocked.exception.code, "DESKTOP_FLOW_ACTION_NOT_ALLOWED")
        with self.assertRaises(PlatformError) as unbound:
            dispatch({"method": "POST", "path": "/v1/application-flows/agent-action", "body": {
                "action_id": "application_flow.inspect_repository",
                "input": {"source_connector_id": connector["connector_id"]},
            }})
        self.assertEqual(unbound.exception.code, "DESKTOP_FLOW_ANALYSIS_CONTEXT_REQUIRED")

    def test_desktop_opportunity_accepts_persisted_manual_hotspot_code(self):
        saved = dispatch({"method": "POST", "path": "/v1/desktop-opportunity/manual-code", "body": {
            "function_hint": "BuildItem::Layout",
            "code": "void BuildItem::Layout() { Measure(); Place(); }\n",
        }})["value"]
        artifact_id = saved["artifact"]["artifact_id"]
        route = dispatch({"method": "POST", "path": "/v1/desktop-opportunity/hardware-route", "body": {
            "manual_code_artifact_id": artifact_id,
            "device_profile": {"vendor": "Huawei", "model": "fixture"},
        }})["value"]
        self.assertEqual(route["source_match_artifact_id"], artifact_id)
        self.assertEqual(route["downstream_context"]["source_evidence"]["source"]["origin"], "user_paste")
        self.assertIn("BuildItem::Layout", route["model_input_context"]["code"])
        self.assertEqual(route["downstream_context"]["trace_evidence"]["evidence_kind"], "manual_code")
        self.assertEqual(route["analysis_artifact_id"], "")
        with self.assertRaises(PlatformError) as too_large:
            dispatch({"method": "POST", "path": "/v1/desktop-opportunity/manual-code", "body": {
                "code": "x" * (16 * 1024 + 1),
            }})
        self.assertEqual(too_large.exception.code, "OPPORTUNITY_MANUAL_CODE_TOO_LARGE")

    def test_desktop_opportunity_pipeline_uses_real_trace_source_and_hardware_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            implementation = source / "frameworks" / "render" / "image_renderer.cpp"
            implementation.parent.mkdir(parents=True)
            implementation.write_text(
                "void ImageRenderer::RenderFrame() {\n"
                "  UploadTexture();\n"
                "  DrawFrame();\n"
                "}\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=Desktop test",
                            "-c", "user.email=desktop@example.invalid", "commit", "-qm", "fixture"], check=True)
            connector = dispatch({"method": "POST", "path": "/v1/source-connectors/local", "body": {
                "source_root": str(source), "display_name": "Render source",
            }})["value"]["connector"]
            trace = ArtifactStore(self.root, project_id="local-default").write(
                "trace_analysis_result",
                {
                    "input_kind": "external_trace_analysis_report",
                    "trace_name": "gallery-render.json",
                    "quality": {"status": "partial"},
                    "clusters": [{"cluster_id": "render"}],
                    "hotspot_candidates": [{
                        "hotspot_id": "render-frame",
                        "function_ref": {"symbol": "ImageRenderer::RenderFrame", "module": "ArkUI",
                                         "resolution": "external_symbol_only"},
                        "active_time_metrics": {"total_duration_ms": 12.5, "call_count": 8},
                        "temporal_pressure": "high",
                    }],
                },
                source_connector_id=connector["connector_id"],
                source_binding={"match_level": "exact", "resolved_snapshot_id": connector["snapshot"]["snapshot_id"]},
                producer={"name": "desktop-opportunity-test", "version": "1.0"},
            )

            catalog = dispatch({"method": "GET", "path": "/v1/desktop-opportunity/traces"})["value"]
            self.assertEqual(catalog["traces"][0]["artifact_id"], trace["artifact_id"])
            detail = dispatch({"method": "GET", "path":
                f"/v1/desktop-opportunity/traces/{trace['artifact_id']}"})["value"]
            self.assertEqual(detail["hotspots"][0]["event_name"], "ImageRenderer::RenderFrame")

            source_match = dispatch({"method": "POST", "path": "/v1/desktop-opportunity/source-match", "body": {
                "analysis_artifact_id": trace["artifact_id"], "hotspot_id": "render-frame",
                "source_connector_id": connector["connector_id"],
            }})["value"]
            self.assertEqual(source_match["quality"]["status"], "available")
            self.assertEqual(source_match["selected_match"]["path"], "frameworks/render/image_renderer.cpp")
            self.assertIn("RenderFrame", source_match["selected_match"]["code"])

            route = dispatch({"method": "POST", "path": "/v1/desktop-opportunity/hardware-route", "body": {
                "analysis_artifact_id": trace["artifact_id"], "hotspot_id": "render-frame",
                "source_match": source_match,
                "device_profile": {"vendor": "Huawei", "model": "fixture", "chipset": "Kirin",
                                   "os_build": "OpenHarmony fixture", "cpu_groups": [{"id": "big"}]},
            }})["value"]
            self.assertEqual(route["schema_version"], "desktop_hardware_model_context/v1")
            self.assertEqual(route["selection_owner"], "right_code_agent_model")
            self.assertEqual(route["selection_status"], "model_required")
            self.assertEqual(route["target_platform"]["os_build"], "OpenHarmony fixture")
            cards = route["model_input_context"]["hardware_capability_cards"]
            self.assertIn("gpu", [item["unit_id"] for item in cards])
            directories = route["api_directories"]
            gpu_directory = next(item for item in directories if item["unit_id"] == "gpu")
            self.assertGreater(gpu_directory["record_count"], 0)
            self.assertGreater(len(gpu_directory["semantic_families"]), 0)
            self.assertIn("RenderFrame", route["model_input_context"]["code"])
            self.assertNotIn("trace_evidence", route["model_input_context"])
            self.assertNotIn("source_evidence", route["model_input_context"])
            self.assertNotIn("api_directories", route["model_input_context"])
            self.assertNotIn("temporal_pressure", route["model_input_context"]["hotspot"]["trace_metrics"])
            self.assertEqual(route["model_input_context"]["hotspot"]["trace_metrics"]["occurrence_count"], 8)
            self.assertIn("trace_evidence", route["downstream_context"])
            self.assertEqual(route["downstream_context"]["trace_evidence"]["temporal_pressure"], "high")
            self.assertNotIn("routes", route)
            self.assertNotIn("selected_units", route)
            self.assertNotIn("matched_terms", json.dumps(route, ensure_ascii=False))
            self.assertNotIn("route_score", json.dumps(route, ensure_ascii=False))
            self.assertTrue(route["artifact"]["artifact_id"].startswith("art_"))
            with self.assertRaises(PlatformError) as untrusted:
                dispatch({"method": "POST", "path": "/v1/desktop-opportunity/hardware-route", "body": {
                    "analysis_artifact_id": trace["artifact_id"], "hotspot_id": "render-frame",
                    "source_match": {"selected_match": {"code": "untrusted client text RenderFrame"}},
                    "device_profile": {},
                }})
            self.assertEqual(untrusted.exception.code, "OPPORTUNITY_SOURCE_MATCH_ARTIFACT_REQUIRED")
            empty_match = ArtifactStore(self.root, project_id="local-default").write(
                "desktop_hotspot_source_match",
                {"analysis_artifact_id": trace["artifact_id"], "hotspot_id": "render-frame",
                 "source_connector_id": connector["connector_id"], "selected_match": None,
                 "source_binding": {"match_level": "unknown"}},
                source_connector_id=connector["connector_id"],
                source_binding={"match_level": "unknown"},
                producer={"name": "desktop-opportunity-test", "version": "1.0"},
            )
            with self.assertRaises(PlatformError) as missing_code:
                dispatch({"method": "POST", "path": "/v1/desktop-opportunity/hardware-route", "body": {
                    "analysis_artifact_id": trace["artifact_id"], "hotspot_id": "render-frame",
                    "source_match": {"artifact": {"artifact_id": empty_match["artifact_id"]}},
                    "device_profile": {},
                }})
            self.assertEqual(missing_code.exception.code, "OPPORTUNITY_SOURCE_CODE_REQUIRED")

    def test_desktop_raw_htrace_opportunity_uses_web_self_time_modules_and_tag_decoding(self):
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        source = Path(fixture.name)
        header = source / "base/hiviewdfx/hitrace/interfaces/native/innerkits/include/hitrace_meter/hitrace_meter.h"
        header.parent.mkdir(parents=True)
        header.write_text(
            "constexpr uint64_t HITRACE_TAG_FFRT = (1ULL << 13); // FFRT\n"
            "constexpr uint64_t HITRACE_TAG_ACE = (1ULL << 39); // ACE\n",
            encoding="utf-8",
        )
        implementation = source / "frameworks/gallery/gallery_builder.cpp"
        implementation.parent.mkdir(parents=True)
        implementation.write_text(
            "constexpr const char* ACE_LAYOUT_TRACE = \"H:AceLayout|I39\";\n"
            "void GalleryBuildFrame() {\n"
            "  // Runtime trace label and source symbol intentionally share this stable name.\n"
            "}\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(source), "-c", "user.name=Desktop test",
            "-c", "user.email=desktop@example.invalid", "commit", "-qm", "HiTrace tags",
        ], check=True)
        connector = dispatch({"method": "POST", "path": "/v1/source-connectors/local", "body": {
            "source_root": str(source), "display_name": "HiTrace source",
        }})["value"]["connector"]

        database = self.root / "htrace-conversions" / "raw.db"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE process(id INTEGER, ipid INTEGER, pid INTEGER, name TEXT);
                CREATE TABLE thread(id INTEGER, itid INTEGER, tid INTEGER, name TEXT, ipid INTEGER);
                CREATE TABLE callstack(id INTEGER, ts INTEGER, dur INTEGER, callid INTEGER, name TEXT, parent_id INTEGER);
                INSERT INTO process VALUES (1, 1, 100, 'system');
                INSERT INTO process VALUES (2, 2, 200, 'com.huawei.hmos.photos');
                INSERT INTO thread VALUES (1, 7, 701, 'OS_FFRT_0', 1);
                INSERT INTO thread VALUES (2, 8, 801, 'main', 2);
                INSERT INTO callstack VALUES (1, 1000000000, 8000000, 7, 'H:FFRTTask|I13', 4294967295);
                INSERT INTO callstack VALUES (2, 1010000000, 10000000, 8, 'H:AceLayout|I39', 4294967295);
                INSERT INTO callstack VALUES (3, 1011000000, 4000000, 8, 'ChildEvent', 2);
                INSERT INTO callstack VALUES (4, 1030000000, 5000000, 8, 'GalleryBuildFrame', 4294967295);
                """
            )
        database_digest = hashlib.sha256(database.read_bytes()).hexdigest()
        artifact = ArtifactStore(self.root, project_id="local-default").write(
            "trace_analysis_result",
            {
                "input_kind": "raw_htrace",
                "trace_name": "gallery-self-time.htrace",
                "quality": {"status": "partial"},
                "raw_htrace": {"conversion": {"database": {
                    "managed_path": str(database.relative_to(self.root)),
                    "sha256": database_digest,
                }}},
                "hotspot_candidates": [
                    {
                        "hotspot_id": "phase-a-ffrt",
                        "function_ref": {
                            "symbol": "H:FFRTTask|I13", "module": "system",
                            "process": "system", "thread": "OS_FFRT_0",
                        },
                        "active_time_metrics": {"total_duration_ms": 8.0, "call_count": 1},
                    },
                    {
                        "hotspot_id": "phase-a-ace",
                        "function_ref": {
                            "symbol": "H:AceLayout|I39", "module": "photos",
                            "process": "com.huawei.hmos.photos", "thread": "main",
                        },
                        "active_time_metrics": {"total_duration_ms": 10.0, "call_count": 1},
                    },
                ],
            },
            source_connector_id=connector["connector_id"],
            source_binding={
                "match_level": "exact",
                "resolved_snapshot_id": connector["snapshot"]["snapshot_id"],
            },
            producer={"name": "desktop-raw-htrace-test", "version": "1.0"},
        )

        detail = dispatch({"method": "GET", "path":
                           f"/v1/desktop-opportunity/traces/{artifact['artifact_id']}"})["value"]
        self.assertEqual(detail["hotspot_quality"]["profile_id"], "htrace-event-self-time-v2")
        self.assertEqual(
            detail["hotspot_quality"]["source"],
            "converted_callstack_direct_child_subtraction",
        )
        self.assertEqual(detail["hotspot_quality"]["hitrace_marker_events"], 2)
        self.assertEqual(detail["hotspot_quality"]["hitrace_tag_catalog"]["status"], "available")
        self.assertEqual(detail["module_catalog"]["classification"], "hitrace_tag_then_trace_identity_v1")
        groups = {item["module_key"]: item for item in detail["module_catalog"]["groups"]}
        self.assertIn("ffrt_runtime", groups)
        self.assertIn("arkui_ui", groups)
        self.assertGreater(len(detail["module_event_groups"]), 0)

        hotspots = {item["event_name"]: item for item in detail["hotspots"]}
        self.assertEqual(hotspots["H:FFRTTask|I13"]["total_duration_ms"], 8.0)
        self.assertEqual(hotspots["H:AceLayout|I39"]["total_duration_ms"], 6.0)
        self.assertEqual(hotspots["H:AceLayout|I39"]["inclusive_total_duration_ms"], 10.0)
        self.assertEqual(
            hotspots["H:AceLayout|I39"]["metric_semantics"],
            "instrumentation_span_direct_child_self_time_estimate",
        )
        self.assertEqual(hotspots["H:FFRTTask|I13"]["module_key"], "ffrt_runtime")
        self.assertEqual(hotspots["H:FFRTTask|I13"]["module_classification"], "hitrace_tag_macro_v1")
        self.assertEqual(
            hotspots["H:FFRTTask|I13"]["hitrace_marker"]["module_tags"][0]["macro"],
            "HITRACE_TAG_FFRT",
        )
        self.assertEqual(hotspots["H:FFRTTask|I13"]["hotspot_id"], "phase-a-ffrt")
        self.assertEqual(hotspots["H:AceLayout|I39"]["hotspot_id"], "phase-a-ace")
        self.assertIsNone(hotspots["H:AceLayout|I39"]["source_path"])

        legacy_source_match = dispatch({
            "method": "POST", "path": "/v1/desktop-opportunity/source-match", "body": {
                "analysis_artifact_id": artifact["artifact_id"],
                "hotspot_id": "phase-a-ace",
                "source_connector_id": connector["connector_id"],
            },
        })["value"]
        self.assertEqual(legacy_source_match["quality"]["status"], "available")
        self.assertEqual(legacy_source_match["event_name"], "H:AceLayout|I39")

        # This self-time event has no legacy Phase-A candidate.  Its Web event
        # ID must remain addressable through source matching and model routing.
        self_time_only = hotspots["GalleryBuildFrame"]
        self.assertEqual(self_time_only["hotspot_id"], self_time_only["event_id"])
        source_match = dispatch({"method": "POST", "path": "/v1/desktop-opportunity/source-match", "body": {
            "analysis_artifact_id": artifact["artifact_id"],
            "hotspot_id": self_time_only["event_id"],
            "source_connector_id": connector["connector_id"],
        }})["value"]
        self.assertEqual(source_match["quality"]["status"], "available")
        self.assertEqual(source_match["hotspot_id"], self_time_only["event_id"])
        self.assertEqual(source_match["event_name"], "GalleryBuildFrame")
        self.assertEqual(source_match["selected_match"]["path"], "frameworks/gallery/gallery_builder.cpp")

        route = dispatch({"method": "POST", "path": "/v1/desktop-opportunity/hardware-route", "body": {
            "analysis_artifact_id": artifact["artifact_id"],
            "hotspot_id": self_time_only["event_id"],
            "source_match": source_match,
            "device_profile": {"os_build": "OpenHarmony raw-htrace fixture"},
        }})["value"]
        self.assertEqual(route["hotspot_id"], self_time_only["event_id"])
        self.assertEqual(route["downstream_context"]["trace_evidence"]["event_name"], "GalleryBuildFrame")
        self.assertEqual(route["downstream_context"]["trace_evidence"]["trace_context"]["thread_name"], "main")
        self.assertEqual(
            route["downstream_context"]["trace_evidence"]["metrics"][
                "self_time_estimate_total_duration_ms"
            ],
            5.0,
        )
        self.assertEqual(
            route["model_input_context"]["hotspot"]["trace_metrics"][
                "self_time_estimate_total_duration_ms"
            ],
            5.0,
        )

    def test_desktop_raw_htrace_opportunity_falls_back_when_self_time_database_is_unavailable(self):
        connector = self.register_source_fixture("Missing raw DB source")
        artifact = ArtifactStore(self.root, project_id="local-default").write(
            "trace_analysis_result",
            {
                "input_kind": "raw_htrace",
                "trace_name": "missing-conversion.htrace",
                "quality": {"status": "partial"},
                "raw_htrace": {"conversion": {"database": {
                    "managed_path": "htrace-conversions/missing.db", "sha256": "d" * 64,
                }}},
                "hotspot_candidates": [{
                    "hotspot_id": "fallback-span",
                    "function_ref": {"symbol": "LegacySpan", "module": "ArkUI"},
                    "active_time_metrics": {"total_duration_ms": 21.0, "call_count": 3},
                }],
            },
            source_connector_id=connector["connector_id"],
            source_binding={
                "match_level": "exact",
                "resolved_snapshot_id": connector["snapshot"]["snapshot_id"],
            },
            producer={"name": "desktop-raw-htrace-test", "version": "1.0"},
        )

        detail = dispatch({"method": "GET", "path":
                           f"/v1/desktop-opportunity/traces/{artifact['artifact_id']}"})["value"]
        self.assertEqual(detail["hotspot_quality"]["status"], "unavailable")
        self.assertEqual(detail["hotspot_quality"]["error_code"], "TRACE_DB_INVALID")
        self.assertEqual(detail["hotspot_quality"]["fallback"], "normalized_hotspot_candidates")
        self.assertTrue(any("not be interpreted as CPU self time" in item
                            for item in detail["hotspot_quality"]["limitations"]))
        self.assertEqual(detail["hotspots"][0]["hotspot_id"], "fallback-span")
        self.assertEqual(detail["hotspots"][0]["total_duration_ms"], 21.0)
        self.assertEqual(detail["module_event_groups"], [])

    def test_desktop_workspace_exposes_builtin_and_custom_hardware_api_catalogs(self):
        npu = dispatch({"method": "GET", "path": "/v1/hardware-kb/npu-api/records?query=conv"})["value"]
        self.assertEqual(npu["npu_api_kb"]["kb_id"], "openharmony_npu_api_knowledge")
        self.assertGreater(npu["record_count"], 0)
        npu_record = npu["records"][0]
        npu_detail = dispatch({"method": "GET", "path":
            f"/v1/hardware-kb/npu-api/records/{npu_record['record_id']}"})["value"]
        self.assertEqual(npu_detail["record"]["record_id"], npu_record["record_id"])

        gpu = dispatch({"method": "GET", "path": "/v1/hardware-kb/accelerator-api/gpu/records"})["value"]
        self.assertEqual(gpu["accelerator_unit"], "gpu")
        self.assertGreater(gpu["record_count"], 0)
        gpu_detail = dispatch({"method": "GET", "path":
            f"/v1/hardware-kb/accelerator-api/gpu/records/{gpu['records'][0]['record_id']}"})["value"]
        self.assertEqual(gpu_detail["accelerator_unit"], "gpu")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            header = source / "interfaces" / "kits" / "include" / "aae_api.h"
            header.parent.mkdir(parents=True)
            header.write_text(
                "typedef struct AAE_Context AAE_Context;\n"
                "int AAE_Process(AAE_Context *ctx, const float *input, float *output, unsigned frames);\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=Desktop test",
                            "-c", "user.email=desktop@example.invalid", "commit", "-qm", "fixture"], check=True)
            connector = dispatch({"method": "POST", "path": "/v1/source-connectors/local", "body": {
                "source_root": str(source), "display_name": "AAE SDK",
            }})["value"]["connector"]
            built = dispatch({"method": "POST", "path": "/v1/custom-api-kbs/build", "body": {
                "unit_name": "AAE", "source_connector_id": connector["connector_id"], "source_root": str(source),
            }})["value"]
            kb_id = built["knowledge_base"]["kb_id"]
            self.assertGreater(built["knowledge_base"]["summary"]["record_count"], 0)
            catalog = dispatch({"method": "GET", "path": "/v1/custom-api-kbs"})["value"]
            self.assertEqual(catalog["knowledge_bases"][0]["kb_id"], kb_id)
            detail = dispatch({"method": "GET", "path": f"/v1/custom-api-kbs/{kb_id}?query=process"})["value"]
            self.assertEqual(detail["records"][0]["symbol"], "AAE_Process")
            record = dispatch({"method": "GET", "path":
                f"/v1/custom-api-kbs/{kb_id}/records/{detail['records'][0]['record_id']}"})["value"]
            self.assertEqual(record["knowledge_base"]["kb_id"], kb_id)
            deleted = dispatch({"method": "DELETE", "path": f"/v1/custom-api-kbs/{kb_id}"})["value"]
            self.assertEqual(deleted["status"], "deleted")

    def _write_workspace_artifact(self, artifact_type, payload):
        return ArtifactStore(self.root).write(
            artifact_type,
            payload,
            source_connector_id="local_fixture",
            source_binding={"declared_revision": "fixture", "resolved_snapshot_id": "fixture"},
            producer={"name": "desktop-rpc-test", "version": "1.0"},
        )

    def test_application_flow_desktop_routes_share_web_registries_and_delete_contract(self):
        discovery = self._write_workspace_artifact("application_flow_discovery", {
            "analysis_context": {"application_flow_analysis_id": "application-flow:" + "1" * 24,
                                 "analysis_goal": "图库编辑", "interaction_iteration": 2},
            "discovery": {"source_snapshot_id": "fixture", "scope": "photos",
                          "flows": [{"id": "edit", "name": "编辑", "order": 1, "description": "", "confidence": "high"}]},
            "quality": {"status": "complete"},
        })
        scenario = self._write_workspace_artifact("application_flow_scenario", {
            "discovery_artifact_id": discovery["artifact_id"], "flow_id": "edit", "flow_name": "编辑",
            "scenario_json": {"scenario": "图库编辑", "stages": [{"id": "open"}]},
            "mermaid": "flowchart LR\n  A --> B", "stage_analysis": {"stages": [{"id": "open"}]},
            "revision": 1,
        })

        updated = dispatch({"method": "PUT", "path": "/v1/application-flows/preferences", "body": {
            "confirm_flow_before_trace": False,
        }})["value"]
        self.assertFalse(updated["confirm_flow_before_trace"])
        preferences = json.loads((self.root / "application-flows/preferences.json").read_text())
        self.assertFalse(preferences["confirm_flow_before_trace"])

        catalog = dispatch({"method": "GET", "path": "/v1/application-flows"})["value"]
        self.assertEqual(catalog["discoveries"][0]["application_flow_analysis_id"], "application-flow:" + "1" * 24)
        self.assertEqual(catalog["discoveries"][0]["interaction_iteration"], 2)
        self.assertEqual(catalog["scenarios"][0]["application_flow_analysis_id"],
                         f"legacy-application-flow:{discovery['artifact_id']}")

        exported = dispatch({"method": "GET", "path": f"/v1/application-flows/scenarios/{scenario['artifact_id']}/mermaid"})
        self.assertEqual(exported["kind"], "text")
        self.assertEqual(exported["filename"], "application-flow.mermaid")
        self.assertIn("A --> B", exported["content"])

        with self.assertRaises(PlatformError) as has_children:
            dispatch({"method": "DELETE", "path": f"/v1/application-flows/application_flow_discovery/{discovery['artifact_id']}"})
        self.assertEqual(has_children.exception.code, "APPLICATION_FLOW_ASSET_HAS_CHILDREN")
        deleted = dispatch({"method": "DELETE", "path":
            f"/v1/application-flows/application_flow_discovery/{discovery['artifact_id']}?cascade=true"})["value"]
        self.assertEqual(deleted["cascade_count"], 1)
        registry = json.loads((self.root / "application-flows/registry.json").read_text())
        self.assertEqual(registry["deleted_assets"][scenario["artifact_id"]]["artifact_type"], "application_flow_scenario")
        catalog = dispatch({"method": "GET", "path": "/v1/application-flows"})["value"]
        self.assertEqual(catalog["discoveries"], [])
        self.assertEqual(catalog["scenarios"], [])

    def test_application_flow_trace_binding_and_deletion_use_web_registry_contract(self):
        trace = self._write_workspace_artifact("trace_analysis_result", {
            "trace_name": "gallery.htrace", "input_kind": "raw_htrace", "analysis_profile": "ui",
            "stage_catalog": [{"id": "build"}], "hotspot_candidates": [], "quality": {"status": "complete"},
        })
        analysis_id = "application-flow:" + "2" * 24
        bound = dispatch({"method": "POST", "path": "/v1/application-flows/runtime-traces/bind", "body": {
            "application_flow_analysis_id": analysis_id, "analysis_artifact_id": trace["artifact_id"],
        }})["value"]
        self.assertEqual(bound["trace"]["artifact_id"], trace["artifact_id"])
        registry_path = self.root / "application-flows/runtime-traces.json"
        registry = json.loads(registry_path.read_text())
        self.assertEqual(registry["analysis_bindings"][analysis_id]["analysis_artifact_id"], trace["artifact_id"])
        deleted = dispatch({"method": "DELETE", "path": f"/v1/application-flows/runtime-traces/{trace['artifact_id']}"})["value"]
        self.assertEqual(deleted["cleared_analysis_binding_count"], 1)
        registry = json.loads(registry_path.read_text())
        self.assertIn(trace["artifact_id"], registry["deleted_traces"])
        self.assertNotIn(analysis_id, registry["analysis_bindings"])
        self.assertEqual(dispatch({"method": "GET", "path": "/v1/application-flows/runtime-traces"})["value"]["traces"], [])

    def test_desktop_opportunity_trace_catalog_reports_visible_total_and_supports_500_rows(self):
        traces = [
            self._write_workspace_artifact("trace_analysis_result", {
                "trace_name": f"gallery-{index:03d}.htrace",
                "input_kind": "raw_htrace",
                "analysis_profile": "ui",
                "stage_catalog": [],
                "hotspot_candidates": [],
                "quality": {"status": "complete"},
            })
            for index in range(500)
        ]

        first_page = dispatch({
            "method": "GET", "path": "/v1/desktop-opportunity/traces?limit=17",
        })["value"]
        self.assertEqual(first_page["count"], 17)
        self.assertEqual(first_page["total_count"], 500)

        expanded = dispatch({
            "method": "GET", "path": "/v1/desktop-opportunity/traces?limit=500",
        })["value"]
        self.assertEqual(expanded["count"], 500)
        self.assertEqual(expanded["total_count"], 500)

        application_flow_catalog = dispatch({
            "method": "GET", "path": "/v1/application-flows/runtime-traces?limit=500",
        })["value"]
        self.assertEqual(application_flow_catalog["count"], 500)
        self.assertEqual(application_flow_catalog["total_count"], 500)

        deleted_id = traces[-1]["artifact_id"]
        dispatch({"method": "DELETE", "path": f"/v1/application-flows/runtime-traces/{deleted_id}"})
        after_delete = dispatch({
            "method": "GET", "path": "/v1/desktop-opportunity/traces?limit=999",
        })["value"]
        self.assertEqual(after_delete["count"], 499)
        self.assertEqual(after_delete["total_count"], 499)
        self.assertNotIn(deleted_id, {row["artifact_id"] for row in after_delete["traces"]})
        application_flow_after_delete = dispatch({
            "method": "GET", "path": "/v1/application-flows/runtime-traces?limit=999",
        })["value"]
        self.assertEqual(application_flow_after_delete["count"], 499)
        self.assertEqual(application_flow_after_delete["total_count"], 499)
        self.assertNotIn(deleted_id, {row["artifact_id"] for row in application_flow_after_delete["traces"]})

    def test_code_knowledge_markdown_uses_desktop_text_transport(self):
        report = self._write_workspace_artifact("code_knowledge_report", {
            "graph_id": "mcg_" + "3" * 24, "title": "图库 BuildItem", "markdown": "# BuildItem\n\n内容",
            "revision": 1,
        })
        downloaded = dispatch({"method": "GET", "path": f"/v1/code-knowledge-reports/{report['artifact_id']}/markdown"})
        self.assertEqual(downloaded["kind"], "text")
        self.assertEqual(downloaded["filename"], "BuildItem.md")
        self.assertEqual(downloaded["content"], "# BuildItem\n\n内容\n")

    def test_code_graph_one_shot_adapter_joins_and_returns_persisted_state(self):
        calls = []

        class Executor:
            def __init__(self, phase): self.phase = phase
            def shutdown(self, *, wait): calls.append((self.phase, wait))

        class Manager:
            _estimate_executor = Executor("estimate")
            _executor = Executor("build")
            @staticmethod
            def get_graph(graph_id):
                return {"graph_id": graph_id, "status": "awaiting_confirmation", "progress": {"phase": "awaiting_confirmation"}}

        result = _wait_for_code_graph_job(
            Manager(), {"status": "accepted", "graph": {"graph_id": "mcg_" + "4" * 24, "status": "estimating"}},
            estimate=True,
        )
        self.assertEqual(calls, [("estimate", True), ("build", True)])
        self.assertEqual(result["graph"]["status"], "awaiting_confirmation")

    def test_code_graph_desktop_route_returns_final_estimate_and_build_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "main.cpp").write_text("int answer() { return 42; }\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "add", "main.cpp"], check=True)
            subprocess.run([
                "git", "-C", str(source), "-c", "user.name=Desktop test",
                "-c", "user.email=desktop@example.invalid", "commit", "-qm", "fixture",
            ], check=True)
            connector = dispatch({"method": "POST", "path": "/v1/source-connectors/local", "body": {
                "source_root": str(source), "display_name": "Tiny C++",
            }})["value"]["connector"]
            estimate = dispatch({"method": "POST", "path": "/v1/code-graphs/estimates", "body": {
                "source_connector_id": connector["connector_id"], "subpath": "", "max_files": 10, "force_new": True,
            }})["value"]
            self.assertEqual(estimate["graph"]["status"], "awaiting_confirmation")
            built = dispatch({"method": "POST", "path":
                f"/v1/code-graphs/{estimate['graph']['graph_id']}/confirm"})["value"]
            self.assertEqual(built["graph"]["status"], "ready")
            loaded = dispatch({"method": "GET", "path":
                f"/v1/code-graphs/{estimate['graph']['graph_id']}"})["value"]["graph"]
            self.assertEqual(loaded["status"], "ready")

    @staticmethod
    def capture_package_bytes(run_id: str = "capture_" + "a" * 24) -> bytes:
        files = {
            "04_build/trace.ftrace": b"trace evidence\n",
            "04_build/pmu.csv": b"timestamp_ns,cycles,instructions\n100,20,10\n",
            "04_build/pmu.json": b'{"target_tid":1,"clock":"monotonic"}\n',
            "04_build/hilog.log": b"BuildItem evidence\n",
        }
        kinds = {"trace.ftrace": "trace", "pmu.csv": "pmu", "pmu.json": "pmu_metadata", "hilog.log": "hilog"}
        manifest = {
            "schema_version": "scenario_capture_manifest/v1", "run_id": run_id,
            "plan_id": "art_" + "b" * 24, "project_id": "desktop-local", "title": "图库17步", "status": "succeeded",
            "created_at": "2026-10-28T00:00:00Z", "device_serial": "PHONE-WIN",
            "steps": [{"step_id": "step_04", "ordinal": 4, "title": "BuildItem 编辑", "captured": True,
                       "collector_status": {"trace": "collected", "pmu": "collected", "hilog": "collected"},
                       "trace_validation": {"valid": True}}],
            "files": [{"kind": kinds[Path(name).name], "step_id": "step_04", "name": Path(name).name,
                       "relative_path": name, "size_bytes": len(content),
                       "sha256": hashlib.sha256(content).hexdigest()} for name, content in files.items()],
            "quality": {"warnings": [], "errors": []},
        }
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest))
            for name, content in files.items():
                bundle.writestr(name, content)
        return stream.getvalue()

    def test_desktop_capture_staging_imports_whole_package_and_removes_upload(self):
        content = self.capture_package_bytes()
        digest = hashlib.sha256(content).hexdigest()
        contract = {"archive_sha256": digest, "archive_size_bytes": len(content)}
        staged = dispatch({"method": "POST", "path": "/v1/desktop-capture/staging", "body": contract})["value"]
        remote = Path(staged["remote_path"])
        self.assertEqual(remote.parent, self.root.resolve() / "desktop-capture-uploads")
        self.assertRegex(staged["upload_token"], r"^[0-9a-f]{32}$")
        remote.write_bytes(content)
        calls = []

        def imported(_manager, payload):
            calls.append(payload)
            return {"schema_version": "parallel_modeling_dataset/v1", "id": "ds_" + "d" * 24,
                    "name": payload["name"], "status": "ready", "source_kind": "capture_manifest",
                    "provenance": {"capture": {"step_id": payload["step_id"]}}}

        with patch.object(ParallelModelingManager, "import_dataset", imported):
            result = dispatch({"method": "POST", "path": "/v1/desktop-capture/import-package", "body": {
                **contract, "upload_token": staged["upload_token"],
            }})["value"]
        self.assertEqual(result["run_id"], "capture_" + "a" * 24)
        self.assertEqual(result["analysis_ready_count"], 1)
        self.assertEqual(result["datasets"][0]["id"], "ds_" + "d" * 24)
        self.assertEqual(result["dataset_imports"][0]["file_kinds"], ["hilog", "pmu", "pmu_metadata", "trace"])
        self.assertEqual(calls[0]["step_id"], "step_04")
        self.assertEqual(calls[0]["name"], "[图库17步] Step 4: BuildItem 编辑")
        self.assertTrue(all(entry["managed_path"].startswith("scenario-capture/runs/capture_")
                            for entry in calls[0]["capture_manifest"]["files"]))
        self.assertFalse(remote.exists())
        self.assertFalse(remote.with_name(staged["upload_token"] + ".reservation.json").exists())
        self.assertTrue((self.root / "scenario-capture" / "runs" / result["run_id"] / "04_build" / "hilog.log").is_file())

    def test_desktop_capture_materializes_runtime_trace_for_application_flow(self):
        connector = self.register_source_fixture("Gallery source")
        trace = b"""\
  photos-101 (  101) [001] .... 100.000000: tracing_mark_write: B|101|H:GalleryOpenEditor|I39
  photos-101 (  101) [001] .... 100.001000: tracing_mark_write: B|101|H:CustomNode:BuildItem|I39
  photos-101 (  101) [001] .... 100.003000: tracing_mark_write: E|101|I39
  photos-101 (  101) [001] .... 100.004000: tracing_mark_write: E|101|I39
"""
        files = {
            "04_build/trace.ftrace": trace,
            "04_build/pmu.csv": b"timestamp_ns,cycles,instructions\n100,20,10\n",
            "04_build/pmu.json": b'{"target_tid":101,"clock":"monotonic"}\n',
            "04_build/hilog.log": b"BuildItem evidence\n",
        }
        kinds = {"trace.ftrace": "trace", "pmu.csv": "pmu", "pmu.json": "pmu_metadata", "hilog.log": "hilog"}
        manifest = {
            "schema_version": "scenario_capture_manifest/v1", "run_id": "capture_" + "f" * 24,
            "plan_id": "art_" + "b" * 24, "project_id": "desktop-local", "title": "图库17步",
            "status": "succeeded", "created_at": "2026-10-28T00:00:00Z", "device_serial": "PHONE-WIN",
            "steps": [{"step_id": "step_04", "ordinal": 4, "title": "BuildItem 编辑", "captured": True,
                       "collector_status": {"trace": "collected"}, "trace_validation": {"valid": True}}],
            "files": [{"kind": kinds[Path(name).name], "step_id": "step_04", "name": Path(name).name,
                       "relative_path": name, "size_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                      for name, value in files.items()],
            "quality": {"warnings": [], "errors": []},
        }
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest))
            for name, value in files.items(): bundle.writestr(name, value)
        content = stream.getvalue(); digest = hashlib.sha256(content).hexdigest()
        contract = {"archive_sha256": digest, "archive_size_bytes": len(content)}
        staged = dispatch({"method": "POST", "path": "/v1/desktop-capture/staging", "body": contract})["value"]
        Path(staged["remote_path"]).write_bytes(content)
        result = dispatch({"method": "POST", "path": "/v1/desktop-capture/import-runtime-trace", "body": {
            **contract, "upload_token": staged["upload_token"], "step_id": "step_04",
            "source_connector_id": connector["connector_id"],
        }})["value"]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["trace"]["trace_name"], "[图库17步] Step 4: BuildItem 编辑")
        self.assertEqual(result["trace"]["hotspot_count"], 2)
        self.assertEqual(result["extraction"]["matched_span_count"], 2)
        catalog = dispatch({"method": "GET", "path": "/v1/application-flows/runtime-traces"})["value"]
        self.assertEqual(catalog["traces"][0]["artifact_id"], result["trace"]["artifact_id"])
        artifact = json.loads((self.root / "artifacts" / "trace_analysis_result" /
                               f"{result['trace']['artifact_id']}.json").read_text())
        self.assertEqual(artifact["input_kind"], "external_trace_analysis_report")
        self.assertEqual(artifact["stage_catalog"][0]["top_events"][0]["name"], "GalleryOpenEditor")
        self.assertTrue(artifact["input_artifacts"][0].startswith("artifact:art_"))
        self.assertNotIn("PHONE-WIN", json.dumps(artifact, ensure_ascii=False))
        self.assertRegex(artifact["capture_provenance"]["device_serial_sha256"], r"^[0-9a-f]{64}$")

        detail = dispatch({"method": "GET", "path":
                           f"/v1/desktop-opportunity/traces/{result['trace']['artifact_id']}"})["value"]
        self.assertEqual(detail["hotspots"][0]["call_count"], 1)
        self.assertGreater(detail["hotspots"][0]["total_duration_ms"], 0)
        self.assertNotIn("raw_candidate", detail["hotspots"][0])
        with self.assertRaises(PlatformError) as source_required:
            dispatch({"method": "POST", "path": "/v1/desktop-opportunity/hardware-route", "body": {
                "analysis_artifact_id": result["trace"]["artifact_id"],
                "hotspot_id": detail["hotspots"][0]["hotspot_id"],
                "source_match": {}, "device_profile": {"os_build": "OpenHarmony fixture"},
            }})
        self.assertEqual(source_required.exception.code, "OPPORTUNITY_SOURCE_MATCH_ARTIFACT_REQUIRED")

        # Uploading the same immutable capture again reuses both content-addressed
        # artifacts instead of duplicating the real-Trace inventory.
        staged_again = dispatch({"method": "POST", "path": "/v1/desktop-capture/staging", "body": contract})["value"]
        Path(staged_again["remote_path"]).write_bytes(content)
        repeated = dispatch({"method": "POST", "path": "/v1/desktop-capture/import-runtime-trace", "body": {
            **contract, "upload_token": staged_again["upload_token"], "step_id": "step_04",
            "source_connector_id": connector["connector_id"],
        }})["value"]
        self.assertEqual(repeated["trace"]["artifact_id"], result["trace"]["artifact_id"])
        self.assertEqual(repeated["raw_input_artifact_id"], result["raw_input_artifact_id"])
        self.assertEqual(len(list((self.root / "artifacts" / "trace_analysis_result").glob("*.json"))), 1)

    def test_desktop_capture_staging_can_be_discarded_without_import_side_effects(self):
        content = self.capture_package_bytes("capture_" + "e" * 24)
        digest = hashlib.sha256(content).hexdigest()
        contract = {"archive_sha256": digest, "archive_size_bytes": len(content)}
        staged = dispatch({"method": "POST", "path": "/v1/desktop-capture/staging", "body": contract})["value"]
        remote = Path(staged["remote_path"])
        remote.write_bytes(content[:64])
        request = {**contract, "upload_token": staged["upload_token"]}

        with self.assertRaises(PlatformError) as mismatch:
            dispatch({"method": "POST", "path": "/v1/desktop-capture/discard-staging", "body": {
                **request, "archive_sha256": "0" * 64,
            }})
        self.assertEqual(mismatch.exception.code, "DESKTOP_CAPTURE_RESERVATION_MISMATCH")
        self.assertTrue(remote.exists())
        self.assertTrue(remote.with_name(staged["upload_token"] + ".reservation.json").exists())

        discarded = dispatch({"method": "POST", "path": "/v1/desktop-capture/discard-staging", "body": request})["value"]
        self.assertEqual(discarded, {"schema_version": "desktop_capture_discard/v1", "status": "discarded", "discarded": True})
        self.assertFalse(remote.exists())
        self.assertFalse(remote.with_name(staged["upload_token"] + ".reservation.json").exists())
        self.assertFalse((self.root / "scenario-capture" / "runs" / ("capture_" + "e" * 24)).exists())

        repeated = dispatch({"method": "POST", "path": "/v1/desktop-capture/discard-staging", "body": request})["value"]
        self.assertEqual(repeated["status"], "absent")
        self.assertFalse(repeated["discarded"])

    def test_desktop_capture_staging_expires_only_old_token_bound_files(self):
        upload_root = self.root / "desktop-capture-uploads"
        upload_root.mkdir()
        old_token = "1" * 32
        old_archive = upload_root / f"{old_token}.capture.zip"
        old_reservation = upload_root / f"{old_token}.reservation.json"
        old_archive.write_bytes(b"partial")
        old_reservation.write_text("{}")
        retained = upload_root / "operator-note.txt"
        retained.write_text("keep")
        old = time.time() - 25 * 60 * 60
        os.utime(old_archive, (old, old)); os.utime(old_reservation, (old, old))

        content = self.capture_package_bytes("capture_" + "9" * 24)
        dispatch({"method": "POST", "path": "/v1/desktop-capture/staging", "body": {
            "archive_sha256": hashlib.sha256(content).hexdigest(), "archive_size_bytes": len(content),
        }})
        self.assertFalse(old_archive.exists())
        self.assertFalse(old_reservation.exists())
        self.assertTrue(retained.is_file())

    def test_desktop_capture_contract_rejects_token_injection_and_mismatch_cleans_staging(self):
        content = self.capture_package_bytes("capture_" + "c" * 24)
        digest = hashlib.sha256(content).hexdigest()
        contract = {"archive_sha256": digest, "archive_size_bytes": len(content)}
        with self.assertRaises(PlatformError) as invalid:
            dispatch({"method": "POST", "path": "/v1/desktop-capture/import-package", "body": {
                **contract, "upload_token": "../../outside",
            }})
        self.assertEqual(invalid.exception.code, "DESKTOP_CAPTURE_TOKEN_INVALID")

        staged = dispatch({"method": "POST", "path": "/v1/desktop-capture/staging", "body": contract})["value"]
        remote = Path(staged["remote_path"]); remote.write_bytes(content)
        with self.assertRaises(PlatformError) as mismatch:
            dispatch({"method": "POST", "path": "/v1/desktop-capture/import-package", "body": {
                "upload_token": staged["upload_token"], "archive_sha256": "0" * 64,
                "archive_size_bytes": len(content),
            }})
        self.assertEqual(mismatch.exception.code, "DESKTOP_CAPTURE_RESERVATION_MISMATCH")
        self.assertFalse(remote.exists())

    def test_desktop_capture_import_rejects_symlink_without_touching_target(self):
        content = self.capture_package_bytes("capture_" + "d" * 24)
        digest = hashlib.sha256(content).hexdigest()
        contract = {"archive_sha256": digest, "archive_size_bytes": len(content)}
        staged = dispatch({"method": "POST", "path": "/v1/desktop-capture/staging", "body": contract})["value"]
        outside = self.root / "outside.capture.zip"; outside.write_bytes(content)
        remote = Path(staged["remote_path"]); remote.symlink_to(outside)
        with self.assertRaises(PlatformError) as unsafe:
            dispatch({"method": "POST", "path": "/v1/desktop-capture/import-package", "body": {
                **contract, "upload_token": staged["upload_token"],
            }})
        self.assertEqual(unsafe.exception.code, "DESKTOP_CAPTURE_UPLOAD_UNSAFE")
        self.assertTrue(outside.is_file())
        self.assertFalse(remote.exists())

    def test_analyze_read_and_export_a_real_builtin_dataset(self):
        manager = ParallelModelingManager(self.root, artifact_store=None)
        builtin_id = manager.catalog()["builtins"][0]["id"]
        dataset = manager.import_dataset({"builtin_id": builtin_id})

        analyzed = dispatch({"method": "POST", "path": "/v1/workload-features/analyze", "body": {"dataset_id": dataset["id"]}})
        profile = analyzed["value"]
        self.assertGreater(len(profile["stages"]), 0)

        loaded = dispatch({"method": "GET", "path": f'/v1/workload-features/profiles/{profile["id"]}'})
        self.assertEqual(loaded["value"]["profile_sha256"], profile["profile_sha256"])

        exported = dispatch({"method": "GET", "path": f'/v1/workload-features/profiles/{profile["id"]}/export.csv?view=stages'})
        self.assertEqual(exported["kind"], "text")
        self.assertIn("text/csv", exported["content_type"])
        self.assertIn("阶段ID", exported["content"])

    def test_route_surface_is_allow_listed_and_errors_are_structured(self):
        with self.assertRaises(PlatformError) as raised:
            dispatch({"method": "POST", "path": "/v1/builds", "body": {}})
        self.assertEqual(raised.exception.code, "DESKTOP_RPC_ROUTE_NOT_ALLOWED")
        result = response_envelope({"schema_version": SCHEMA_VERSION, "method": "GET", "path": "/not-allowed"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "DESKTOP_RPC_ROUTE_NOT_ALLOWED")

    def test_full_pmu_builtin_response_is_lossless_and_below_desktop_wire_budget(self):
        manager = ParallelModelingManager(self.root, artifact_store=None)
        builtin = next(
            item for item in manager.catalog()["builtins"]
            if item["id"] == "gallery-edit-all-pmu-20261023"
        )
        dataset = manager.import_dataset({"builtin_id": builtin["id"]})
        response = response_envelope({
            "schema_version": SCHEMA_VERSION,
            "method": "POST",
            "path": "/v1/workload-features/analyze",
            "body": {"dataset_id": dataset["id"]},
        })

        line = encode_response_line(response)

        self.assertTrue(line.startswith(RPC_GZIP_MARKER))
        encoded = line.removeprefix(RPC_GZIP_MARKER)
        self.assertLessEqual(len(encoded.encode("ascii")), MAX_RESPONSE_WIRE_BYTES)
        decoded = json.loads(gzip.decompress(base64.b64decode(encoded)))
        self.assertEqual(
            decoded["result"]["value"]["profile_sha256"],
            response["result"]["value"]["profile_sha256"],
        )
        self.assertEqual(
            len(decoded["result"]["value"]["stages"]),
            len(response["result"]["value"]["stages"]),
        )

    def test_parallel_modeling_runs_inline_and_returns_bounded_views(self):
        catalog = dispatch({"method": "GET", "path": "/v1/parallel-modeling/catalog"})["value"]
        dataset = dispatch({
            "method": "POST",
            "path": "/v1/parallel-modeling/datasets",
            "body": {"builtin_id": catalog["builtins"][0]["id"]},
        })["value"]
        analysis = dispatch({
            "method": "POST",
            "path": "/v1/parallel-modeling/analyses",
            "body": {"dataset_id": dataset["id"]},
        })["value"]
        self.assertTrue(analysis["replay_gate"]["passed"])
        self.assertTrue(analysis["graphs"]["observed"]["nodes"])
        self.assertTrue(analysis["graphs"]["parallel"]["nodes"])
        self.assertTrue(analysis["transport_projection"]["full_result_persisted"])
        self.assertNotIn("graphs", analysis["transport_projection"]["omitted"])
        self.assertNotIn("timeline", analysis["baseline"])
        self.assertIn("app_off_cpu_ms", analysis["baseline"])
        self.assertIn("time_accounting", analysis["baseline"])
        self.assertEqual(analysis["baseline"]["app_off_cpu_ms"],
                         analysis["baseline"]["time_accounting"]["observed_reference"]["app_off_cpu_ms"])
        analysis_line = encode_response_line({
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "result": {"kind": "json", "value": analysis},
        })
        self.assertTrue(analysis_line.startswith(RPC_GZIP_MARKER))
        self.assertLessEqual(
            len(analysis_line.removeprefix(RPC_GZIP_MARKER).encode("ascii")),
            MAX_RESPONSE_WIRE_BYTES,
        )

        search = dispatch({
            "method": "POST",
            "path": "/v1/parallel-modeling/searches",
            "body": {
                "analysis_id": analysis["id"],
                "candidate_ids": [item["id"] for item in analysis["components"]],
                "objective": "minimum_latency",
                "space": {
                    "available_core_group_ids": ["big", "middle"],
                    "main_core_group_id": "big",
                    "worker_counts": [0, 1],
                    "main_participation": [False],
                    "frequency_policy": "max_opps",
                },
                "robustness": False,
            },
        })["value"]
        self.assertTrue(search["best"])
        for field in ("graph", "timeline", "critical_chain", "component_costs", "resource_costs"):
            self.assertIn(field, search["best"])
        self.assertNotIn("graph", search["candidates"][0])
        self.assertTrue(search["transport_projection"]["full_result_persisted"])
        search_line = encode_response_line({
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "result": {"kind": "json", "value": search},
        })
        self.assertTrue(search_line.startswith(RPC_GZIP_MARKER))
        self.assertLessEqual(
            len(search_line.removeprefix(RPC_GZIP_MARKER).encode("ascii")),
            MAX_RESPONSE_WIRE_BYTES,
        )
        decoded_search = json.loads(gzip.decompress(base64.b64decode(
            search_line.removeprefix(RPC_GZIP_MARKER)
        )))
        decoded_best = decoded_search["result"]["value"]["best"]
        for field in ("graph", "timeline", "critical_chain", "component_costs", "resource_costs"):
            self.assertEqual(decoded_best[field], search["best"][field])
        loaded = dispatch({"method": "GET", "path": f'/v1/parallel-modeling/searches/{search["id"]}'})["value"]
        self.assertEqual(loaded["id"], search["id"])

        analysis_rows = dispatch({"method": "GET", "path": "/v1/parallel-modeling/analyses"})["value"]["analyses"]
        search_rows = dispatch({"method": "GET", "path": "/v1/parallel-modeling/searches"})["value"]["searches"]
        self.assertNotIn("components", analysis_rows[0])
        self.assertNotIn("candidates", search_rows[0])

    def test_search_projection_caps_candidates_and_removes_heavy_fields(self):
        candidates = [
            {"configuration_index": index, "metrics": {"finish_ms": index + 1}, "graph": {"nodes": [index]},
             "timeline": [index], "critical_chain": [str(index)], "component_costs": [{"id": index}],
             "resource_costs": {"application_lanes": []}}
            for index in range(120)
        ]
        result = _search_view({"id": "sr_" + "1" * 24, "best": candidates[0], "candidates": candidates})
        self.assertEqual(len(result["candidates"]), 100)
        self.assertEqual(result["transport_projection"]["total_candidates"], 120)
        for field in ("graph", "timeline", "critical_chain", "component_costs", "resource_costs"):
            self.assertIn(field, result["best"])
        self.assertNotIn("graph", result["candidates"][0])

    def test_calibration_plan_bundle_and_compact_history_are_allowlisted(self):
        catalog = dispatch({"method": "GET", "path": "/v1/parallel-calibration/catalog"})["value"]
        plan = dispatch({
            "method": "POST",
            "path": "/v1/parallel-calibration/plans",
            "body": {
                "name": "Desktop 校准方案",
                "hardware_profile_id": catalog["defaults"]["hardware_profile_id"],
                "parameter_groups": ["dispatch_overhead"],
                "repeats": 3,
            },
        })["value"]
        self.assertTrue(plan["id"].startswith("cp_"))
        listed = dispatch({"method": "GET", "path": "/v1/parallel-calibration/plans"})["value"]["plans"][0]
        self.assertGreater(listed["condition_count"], 0)
        self.assertNotIn("conditions", listed)
        loaded = dispatch({"method": "GET", "path": f'/v1/parallel-calibration/plans/{plan["id"]}'})["value"]
        self.assertEqual(loaded["id"], plan["id"])
        bundle = dispatch({"method": "GET", "path": f'/v1/parallel-calibration/plans/{plan["id"]}/bundle'})["value"]
        self.assertGreaterEqual(len(bundle["files"]), 4)
        self.assertTrue(any(item["filename"] == "collect.py" for item in bundle["files"]))

        with self.assertRaises(PlatformError) as raised:
            dispatch({"method": "POST", "path": "/v1/parallel-calibration/fits", "body": {}})
        self.assertEqual(raised.exception.code, "DESKTOP_RPC_ROUTE_NOT_ALLOWED")

    def test_calibration_fit_projection_keeps_compact_frequency_curve_observations(self):
        fit = {
            "id": "cf_" + "1" * 24,
            "plan_id": "cp_" + "2" * 24,
            "quality": {"passed": True, "rejected_sample_details": [
                {"index": 2, "sample_id": "rejected"}, {"index": 3, "sample_id": None},
            ]},
            "measurements": {"samples": [
                {"sample_id": "a", "benchmark": "pair_work", "active_tasks": 1,
                 "workload_id": "frequency_proxy_compute", "core_kind": "middle",
                 "workload_class": "compute", "frequency_khz": 1000000,
                 "cpu_ms": 20.0, "work_units": 10.0, "split": "train",
                 "workload_version": "proxy-v7", "working_set_bytes": 67108864,
                 "pmu_window_aligned": True, "pmu_retired_instructions": 4000,
                 "pmu_dram_bytes": 2000, "pmu_collection_status": "collected",
                 "pmu_ipc": 2.0, "pmu_cache_misses": 10.0,
                 "pmu_cache_references": 40.0, "pmu_cache_mpki": 2.5},
                {"sample_id": "b", "benchmark": "pair_work", "active_tasks": 1,
                 "workload_id": "frequency_proxy_compute", "core_kind": "middle",
                 "workload_class": "compute", "frequency_khz": 1000000,
                 "cpu_ms": 80.0, "work_units": 20.0, "split": "train",
                 "workload_version": "proxy-v7", "working_set_bytes": 67108864,
                 "instruction_intensity_instructions_per_dram_byte": 4.0,
                 "pmu_collection_status": "partial", "pmu_ipc": 3.0,
                 "pmu_cache_misses": 40.0},
                {"sample_id": "rejected", "benchmark": "pair_work", "active_tasks": 1,
                 "workload_id": "frequency_proxy_compute", "core_kind": "middle",
                 "workload_class": "compute", "frequency_khz": 1000000,
                 "cpu_ms": 999.0, "work_units": 10.0, "split": "train"},
                {"benchmark": "pair_work", "active_tasks": 1,
                 "workload_id": "frequency_proxy_compute", "core_kind": "middle",
                 "workload_class": "compute", "frequency_khz": 1000000,
                 "cpu_ms": 888.0, "work_units": 10.0, "split": "train"},
                {"sample_id": "other", "benchmark": "pair_work", "active_tasks": 2,
                 "workload_id": "pairwise", "core_kind": "middle", "workload_class": "compute",
                 "frequency_khz": 1000000, "cpu_ms": 1.0, "work_units": 1.0},
            ]},
            "validation_predictions": [{"large": "omitted"}],
        }
        summary = _fit_summary(fit)
        projection = summary["frequency_curve_observations"]
        self.assertEqual(projection["schema_version"],
                         "calibration_frequency_curve_observations/v5")
        self.assertEqual(projection["accepted_sample_count"], 2)
        self.assertEqual(projection["points"], [{
            "core_kind": "middle", "workload_class": "compute", "frequency_khz": 1000000,
            "unit_cpu_ms": 3.0, "repeat_count": 2, "splits": ["train"],
            "workload_ids": ["frequency_proxy_compute"], "workload_versions": ["proxy-v7"],
            "working_set_bytes": [67108864],
            "instruction_intensity_instructions_per_dram_byte": 3.0,
            "instruction_intensity_sample_count": 2,
            "pmu_status": "partial", "pmu_ipc": 2.5,
            "pmu_cache_misses": 25.0, "pmu_cache_references": 40.0,
            "pmu_cache_misses_per_work_unit": 1.5,
            "pmu_cache_mpki": 2.5,
            "pmu_metric_sample_counts": {"pmu_ipc": 2, "pmu_cache_misses": 2,
                                         "pmu_cache_references": 1,
                                         "pmu_cache_mpki": 1},
            "pmu_sample_count": 1,
        }])
        view = _fit_view(fit)
        self.assertNotIn("measurements", view)
        self.assertNotIn("validation_predictions", view)
        self.assertEqual(view["frequency_curve_observations"], projection)

    def test_calibration_measurement_staging_fits_real_payload_and_cleans_files(self):
        from backend.tests.test_parallel_calibration import build_measurements

        catalog = dispatch({"method": "GET", "path": "/v1/parallel-calibration/catalog"})["value"]
        plan = dispatch({"method": "POST", "path": "/v1/parallel-calibration/plans", "body": {
            "name": "Desktop staging fit", "hardware_profile_id": catalog["defaults"]["hardware_profile_id"],
            "parameter_groups": ["dispatch_overhead"], "repeats": 3,
        }})["value"]
        content = json.dumps(build_measurements(plan), ensure_ascii=False).encode()
        contract = {"plan_id": plan["id"], "measurement_size_bytes": len(content),
                    "measurement_sha256": hashlib.sha256(content).hexdigest(), "filename": "actual-measurements.json"}
        staged = dispatch({"method": "POST", "path": "/v1/parallel-calibration/measurement-staging",
                           "body": contract})["value"]
        remote = Path(staged["remote_path"])
        self.assertEqual(remote.parent, self.root.resolve() / "parallel-calibration-measurement-uploads")
        self.assertRegex(staged["upload_token"], r"^[0-9a-f]{32}$")
        remote.write_bytes(content)
        request = {**contract, "upload_token": staged["upload_token"]}
        fitted = dispatch({"method": "POST", "path": "/v1/parallel-calibration/fits/from-staging",
                           "body": request})["value"]
        self.assertEqual(fitted["status"], "passed", fitted["quality"])
        self.assertTrue(fitted["transport_projection"]["full_result_persisted"])
        self.assertNotIn("measurements", fitted)
        self.assertFalse(remote.exists())
        self.assertFalse(remote.with_name(staged["upload_token"] + ".reservation.json").exists())

        staged = dispatch({"method": "POST", "path": "/v1/parallel-calibration/measurement-staging",
                           "body": contract})["value"]
        Path(staged["remote_path"]).write_bytes(content[:10])
        discarded = dispatch({"method": "POST", "path": "/v1/parallel-calibration/measurement-staging/discard",
                              "body": {key: value for key, value in {**contract, "upload_token": staged["upload_token"]}.items()
                                       if key != "plan_id"}})["value"]
        self.assertTrue(discarded["discarded"])

    def test_calibration_measurement_fit_reads_the_verified_open_file_description(self):
        from backend.tests.test_parallel_calibration import build_measurements

        catalog = dispatch({"method": "GET", "path": "/v1/parallel-calibration/catalog"})["value"]
        plan = dispatch({"method": "POST", "path": "/v1/parallel-calibration/plans", "body": {
            "name": "Desktop descriptor-bound fit", "hardware_profile_id": catalog["defaults"]["hardware_profile_id"],
            "parameter_groups": ["dispatch_overhead"], "repeats": 3,
        }})["value"]
        content = json.dumps(build_measurements(plan), ensure_ascii=False).encode()
        contract = {"plan_id": plan["id"], "measurement_size_bytes": len(content),
                    "measurement_sha256": hashlib.sha256(content).hexdigest(), "filename": "measurements.json"}
        staged = dispatch({"method": "POST", "path": "/v1/parallel-calibration/measurement-staging",
                           "body": contract})["value"]
        remote = Path(staged["remote_path"])
        remote.write_bytes(content)
        request = {**contract, "upload_token": staged["upload_token"]}
        original_open = os.open
        swapped = False
        measurement_open_flags = None

        def open_then_replace(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped, measurement_open_flags
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            if Path(path) == remote:
                measurement_open_flags = flags
                replacement = remote.with_suffix(".replacement")
                replacement.write_bytes(b"{}")
                os.replace(replacement, remote)
                swapped = True
            return descriptor

        with patch("backend.tools.desktop_platform_rpc.os.open", side_effect=open_then_replace):
            fitted = dispatch({"method": "POST", "path": "/v1/parallel-calibration/fits/from-staging",
                               "body": request})["value"]
        self.assertTrue(swapped)
        self.assertTrue(measurement_open_flags & os.O_NOFOLLOW)
        self.assertEqual(fitted["status"], "passed", fitted["quality"])
        self.assertFalse(remote.exists())


if __name__ == "__main__":
    unittest.main()
