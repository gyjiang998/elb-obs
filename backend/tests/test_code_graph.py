from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.code_graph import CodeGraphManager
from backend.core import ArtifactStore, PlatformError, SourceConnectorManager


class CodeGraphManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.source_root = self.root / "camera-module"
        source = self.source_root / "src" / "camera_flow.cpp"
        source.parent.mkdir(parents=True)
        source.write_text(
            """#include \"camera_flow.h\"

void ConfigureSession() {}

void StartCamera()
{
    HITRACE_METER_NAME(0, \"CameraStart\");
    ConfigureSession();
}
""",
            encoding="utf-8",
        )
        (source.parent / "camera_flow.h").write_text(
            "void ConfigureSession();\nvoid StartCamera();\n",
            encoding="utf-8",
        )
        self.store = ArtifactStore(self.state_root, project_id="local-default")
        self.connectors = SourceConnectorManager(self.state_root, self.store, workspace_root=self.root)
        self.connectors.registry.write(
            {
                "connectors": [
                    {
                        "connector_id": "local_camera_graph",
                        "kind": "local",
                        "mode": "local_readonly",
                        "display_name": "Camera module",
                        "source_root": str(self.source_root.resolve()),
                        "snapshot": {"snapshot_id": "source_snapshot_camera_v1", "clean": True, "repositories": []},
                        "worktree_fingerprint": {"sha256": "a" * 64},
                    }
                ]
            }
        )
        self.manager = CodeGraphManager(self.state_root, self.connectors, self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build(self) -> dict[str, object]:
        response = self.manager.start_build("local_camera_graph", subpath="src", max_files=100)
        graph_id = str(response["graph"]["graph_id"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            graph = self.manager.get_graph(graph_id)
            if graph["status"] in {"ready", "failed"}:
                break
            time.sleep(0.02)
        self.assertEqual(graph["status"], "ready", graph.get("error"))
        return graph

    def _wait_for_status(self, graph_id: str, statuses: set[str], timeout: float = 10) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        graph: dict[str, object] = {}
        while time.monotonic() < deadline:
            graph = self.manager.get_graph(graph_id)
            if graph.get("status") in statuses:
                return graph
            time.sleep(0.02)
        self.fail(f"graph {graph_id} did not reach {statuses}: {graph}")

    def test_builds_snapshot_bound_sqlite_graph_and_searches_one_hop_calls(self) -> None:
        graph = self._build()
        summary = graph["summary"]
        self.assertEqual(summary["files_parsed"], 2)
        self.assertGreaterEqual(summary["symbol_count"], 4)
        self.assertGreaterEqual(summary["call_candidate_count"], 2)
        self.assertEqual(summary["tracepoint_count"], 1)
        self.assertRegex(summary["parsed_source_manifest_sha256"], r"^[a-f0-9]{64}$")

        result = self.manager.search(str(graph["graph_id"]), query="StartCamera", limit=10)
        definition = next(item for item in result["symbols"] if item["kind"] == "function_definition")
        self.assertEqual(definition["path"], "src/camera_flow.cpp")
        self.assertTrue(any(item["callee_text"] == "ConfigureSession" for item in definition["outgoing_calls"]))
        self.assertEqual(result["evidence_scope"], "static_syntax_candidate")

        traces = self.manager.search(str(graph["graph_id"]), query="CameraStart", limit=10)
        self.assertEqual(traces["tracepoints"][0]["label"], "CameraStart")

        artifact = self.store.read("module_code_graph", str(graph["artifact_id"]))
        self.assertEqual(artifact["source_snapshot_id"], "source_snapshot_camera_v1")
        self.assertEqual(artifact["scope"], "src")

    def test_long_fluent_call_chain_has_unique_ast_range_ids(self) -> None:
        chain = ".SetValue".join(f"({index})" for index in range(90))
        source = f"void Build(Event &event) {{ event.SetValue{chain}; }}\n".encode()
        path = self.source_root / "src" / "long_fluent_chain.cpp"
        path.write_bytes(source)

        _, _, calls, _, _ = self.manager._extract_file(
            repository_root=self.source_root,
            path=path,
            source=source,
            parser=self.manager._parsers()["cpp"],
        )

        call_ids = [str(item["call_id"]) for item in calls]
        self.assertGreater(len(call_ids), 80)
        self.assertEqual(len(call_ids), len(set(call_ids)))
        self.assertTrue(any(len(str(item["callee_text"])) == 500 for item in calls))

    def test_tracepoint_navigation_indexes_module_and_arkts_style_wrappers(self) -> None:
        source = (
            b"void Render() {\n"
            b"  EFFECT_TRACE_NAME(\"ImageEffect::Render\");\n"
            b"  Trace.begin('GalleryEdit');\n"
            b"  hiTraceMeter.startAsyncTrace(`SavePhoto`, 1);\n"
            b"  Trace_Start();\n"
            b"}\n"
        )
        path = self.source_root / "src" / "trace_wrappers.cpp"
        path.write_bytes(source)

        _, _, _, _, traces = self.manager._extract_file(
            repository_root=self.source_root,
            path=path,
            source=source,
            parser=self.manager._parsers()["cpp"],
        )

        by_macro = {item["macro"]: item for item in traces}
        self.assertEqual(by_macro["EFFECT_TRACE_NAME"]["label"], "ImageEffect::Render")
        self.assertEqual(by_macro["Trace.begin"]["label"], "GalleryEdit")
        self.assertEqual(by_macro["hiTraceMeter.startAsyncTrace"]["label"], "SavePhoto")
        self.assertIn("Trace_Start", by_macro)

    def test_same_line_entities_have_unique_source_range_ids(self) -> None:
        source = (
            b"void Overloaded(int); void Overloaded(float);\n"
            b"struct Size {}; struct Api { void (*set)(const struct Size* a, const struct Size* b); };\n"
            b"void Trace() { HITRACE_METER_NAME(0, \"Same\"); HITRACE_METER_NAME(0, \"Same\"); }\n"
        )
        path = self.source_root / "src" / "same_line_entities.cpp"
        path.write_bytes(source)

        _, symbols, calls, includes, traces = self.manager._extract_file(
            repository_root=self.source_root,
            path=path,
            source=source,
            parser=self.manager._parsers()["cpp"],
        )

        for rows, id_key in (
            (symbols, "symbol_id"),
            (calls, "call_id"),
            (includes, "include_id"),
            (traces, "tracepoint_id"),
        ):
            entity_ids = [str(item[id_key]) for item in rows]
            self.assertEqual(len(entity_ids), len(set(entity_ids)), id_key)
        self.assertEqual(len([item for item in symbols if item["name"] == "Overloaded"]), 2)
        self.assertEqual(len(traces), 2)

    def test_rejects_graph_reuse_for_another_scope(self) -> None:
        graph = self._build()
        with self.assertRaises(PlatformError) as caught:
            self.manager.validate_ready_graph(str(graph["graph_id"]), scope="")
        self.assertEqual(caught.exception.code, "CODE_GRAPH_SCOPE_MISMATCH")

    def test_estimates_before_confirmation_and_updates_runtime_eta(self) -> None:
        response = self.manager.request_estimate("local_camera_graph", subpath="src", max_files=100)
        graph_id = str(response["graph"]["graph_id"])
        estimated = self._wait_for_status(graph_id, {"awaiting_confirmation", "failed"})
        self.assertEqual(estimated["status"], "awaiting_confirmation", estimated.get("error"))
        estimate = estimated["estimate"]
        self.assertEqual(estimate["schema_version"], "module_code_graph_estimate/v1")
        self.assertEqual(estimate["source_file_count"], 2)
        self.assertGreater(estimate["source_bytes"], 0)
        self.assertTrue(estimate["can_start"])

        confirmed = self.manager.confirm_build(graph_id)
        self.assertEqual(confirmed["status"], "accepted")
        ready = self._wait_for_status(graph_id, {"ready", "failed"})
        self.assertEqual(ready["status"], "ready", ready.get("error"))
        self.assertEqual(ready["progress"]["phase"], "completed")
        self.assertEqual(ready["progress"]["symbol_count"], ready["summary"]["symbol_count"])
        self.assertEqual(ready["progress"]["call_candidate_count"], ready["summary"]["call_candidate_count"])
        self.assertEqual(ready["progress"]["tracepoint_count"], ready["summary"]["tracepoint_count"])
        self.assertGreaterEqual(float(ready["duration_ms"]), 0)

    def test_force_new_build_creates_a_distinct_version_with_parent_lineage(self) -> None:
        first = self._build()
        response = self.manager.request_estimate(
            "local_camera_graph",
            subpath="src",
            max_files=100,
            force_new=True,
        )
        second_id = str(response["graph"]["graph_id"])
        self.assertNotEqual(second_id, first["graph_id"])
        estimated = self._wait_for_status(second_id, {"awaiting_confirmation", "failed"})
        self.assertEqual(estimated["status"], "awaiting_confirmation", estimated.get("error"))
        self.assertEqual(estimated["parent_graph_id"], first["graph_id"])
        self.assertEqual(estimated["build_series_id"], first["graph_id"])
        self.manager.cancel_build(second_id)

    def test_running_build_can_be_cancelled_cooperatively(self) -> None:
        for index in range(80):
            (self.source_root / "src" / f"generated_{index}.cpp").write_text(
                f"int Generated{index}() {{ return {index}; }}\n",
                encoding="utf-8",
            )
        original = self.manager._extract_file
        entered = threading.Event()

        def slow_extract(*args: object, **kwargs: object) -> object:
            entered.set()
            time.sleep(0.01)
            return original(*args, **kwargs)

        with patch.object(self.manager, "_extract_file", side_effect=slow_extract):
            response = self.manager.start_build("local_camera_graph", subpath="src", max_files=1000)
            graph_id = str(response["graph"]["graph_id"])
            self.assertTrue(entered.wait(2))
            cancelled = self.manager.cancel_build(graph_id)
            self.assertEqual(cancelled["status"], "accepted")
            terminal = self._wait_for_status(graph_id, {"cancelled", "failed"})
        self.assertEqual(terminal["status"], "cancelled", terminal.get("error"))
        self.assertFalse((self.manager.database_root / f"{graph_id}.sqlite3").exists())

    def test_agent_report_is_versioned_and_materialized_as_markdown(self) -> None:
        graph = self._build()
        graph_id = str(graph["graph_id"])
        claim = self.manager.claim_semantic_analysis(graph_id, request_id="request_initial")
        self.assertTrue(claim["dispatch_required"])
        duplicate_claim = self.manager.claim_semantic_analysis(graph_id, request_id="request_duplicate")
        self.assertFalse(duplicate_claim["dispatch_required"])
        first = self.manager.publish_report(
            graph_id,
            title="Camera 模块概览",
            markdown="# Camera 模块\n\n```mermaid\ngraph LR\nA --> B\n```",
            agent_run_id="run_report_test",
        )
        second = self.manager.publish_report(
            graph_id,
            title="Camera 启动路径",
            markdown="# Camera 启动路径\n\n`StartCamera` 调用 `ConfigureSession`。",
            question="启动路径是什么？",
            parent_report_id=str(first["artifact_id"]),
            agent_run_id="run_report_test",
        )
        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["parent_report_id"], first["artifact_id"])
        self.assertTrue(self.manager.report_markdown_path(str(second["artifact_id"])).is_file())
        reports = self.manager.list_reports(graph_id=graph_id)
        self.assertEqual([item["artifact_id"] for item in reports[:2]], [second["artifact_id"], first["artifact_id"]])
        completed_claim = self.manager.claim_semantic_analysis(graph_id, request_id="request_after_report")
        self.assertFalse(completed_claim["dispatch_required"])

    def test_reports_and_completed_graphs_are_logically_deleted(self) -> None:
        graph = self._build()
        graph_id = str(graph["graph_id"])
        report = self.manager.publish_report(
            graph_id,
            title="待删除报告",
            markdown="# 待删除报告",
            agent_run_id="run_delete_test",
        )
        report_id = str(report["artifact_id"])

        deleted_report = self.manager.delete_report(report_id)
        self.assertEqual(deleted_report["status"], "deleted")
        self.assertEqual(self.manager.list_reports(graph_id=graph_id), [])
        with self.assertRaises(PlatformError) as report_error:
            self.manager.read_report(report_id)
        self.assertEqual(report_error.exception.code, "CODE_KNOWLEDGE_REPORT_NOT_FOUND")
        self.assertEqual(self.store.read("code_knowledge_report", report_id)["title"], "待删除报告")

        replacement = self.manager.publish_report(
            graph_id,
            title="随底图删除",
            markdown="# 随底图删除",
            agent_run_id="run_delete_test",
        )
        self.assertEqual(replacement["revision"], 2)
        deleted_graph = self.manager.delete_graph(graph_id)
        self.assertEqual(deleted_graph["deleted_report_count"], 1)
        self.assertFalse((self.manager.database_root / f"{graph_id}.sqlite3").exists())
        self.assertFalse(any(item["graph_id"] == graph_id for item in self.manager.list_graphs()))
        self.assertEqual(self.manager.list_reports(graph_id=graph_id), [])
        self.assertEqual(self.store.read("code_knowledge_report", str(replacement["artifact_id"]))["title"], "随底图删除")

    def test_queries_bounded_static_graph_and_publishes_source_bound_report(self) -> None:
        graph = self._build()
        graph_id = str(graph["graph_id"])
        result = self.manager.query_knowledge(graph_id, query="StartCamera", limit=10)

        self.assertEqual(result["provider"], "platform-tree-sitter")
        self.assertEqual(result["retrieval_status"], "ready")
        self.assertEqual(result["retrieval"]["method"], "bounded_static_symbol_call_trace_query/v1")
        self.assertGreater(result["result_count"], 0)
        self.assertTrue(any(item["name"] == "StartCamera" for item in result["static_graph"]["symbols"]))

        report = self.manager.publish_report(
            graph_id,
            title="相机模块报告",
            markdown="# 相机模块\n\n模块入口与调用关系已经过源码复核。",
            agent_run_id="run_static_graph",
        )
        self.assertEqual(report["knowledge_engine"]["engine_id"], "platform-tree-sitter")
        self.assertEqual(report["evidence_scope"], "agent_semantic_analysis_over_static_code_graph")
        self.assertIn(graph["artifact_id"], report["input_artifacts"])


if __name__ == "__main__":
    unittest.main()
