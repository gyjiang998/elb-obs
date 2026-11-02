from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.core import ArtifactStore, PlatformError, SourceConnectorManager
from backend.experimental_flow import ExperimentalFlowManager
from backend.runtime_action_router import PlatformRuntimeActionRouter


class _Gateway:
    pass


class ExperimentalFlowManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.source_root = self.root / "source"
        self.source_root.mkdir(parents=True)
        (self.source_root / "flow.cpp").write_text("void RunFlow() {}\n", encoding="utf-8")
        self.related_source_root = self.root / "related-source"
        self.related_source_root.mkdir(parents=True)
        (self.related_source_root / "service.cpp").write_text("void RunService() {}\n", encoding="utf-8")
        self.store = ArtifactStore(self.state_root, project_id="local-default")
        self.connectors = SourceConnectorManager(self.state_root, self.store, workspace_root=self.root)
        self.connector = {
            "connector_id": "local_flow_test",
            "kind": "local",
            "mode": "local_readonly",
            "display_name": "Flow test",
            "source_root": str(self.source_root.resolve()),
            "snapshot": {"snapshot_id": "source_snapshot_flow_v1", "clean": True, "repositories": []},
            "worktree_fingerprint": {"sha256": "a" * 64},
        }
        self.related_connector = {
            "connector_id": "local_flow_related",
            "kind": "local",
            "mode": "local_readonly",
            "display_name": "Related flow test",
            "source_root": str(self.related_source_root.resolve()),
            "snapshot": {"snapshot_id": "source_snapshot_related_v1", "clean": True, "repositories": []},
            "worktree_fingerprint": {"sha256": "c" * 64},
        }
        self.connectors.registry.write({"connectors": [self.connector, self.related_connector]})
        self.manager = ExperimentalFlowManager(self.state_root, self.connectors, self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def evidence(self, analysis_kind: str = "hiperf") -> dict[str, object]:
        path = self.state_root / "raw-inputs" / f"{analysis_kind}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Self  Command  Shared Object  Symbol\n42.1% app libflow.so RunFlow\n", encoding="utf-8")
        return self.manager.register_evidence(
            analysis_kind=analysis_kind,
            source_connector_id="local_flow_test",
            managed_path=str(path.relative_to(self.state_root)),
            file_name=path.name,
            content_sha256="b" * 64,
            size_bytes=path.stat().st_size,
            media_type="text/plain",
        )

    def test_hiperf_and_trace_assets_remain_separate(self) -> None:
        hiperf = self.evidence("hiperf")
        trace = self.evidence("trace")

        result = self.manager.publish_result(
            analysis_kind="hiperf",
            source_connector_id="local_flow_test",
            flow_goal="打开页面并完成一次处理",
            title="HiPerf 函数流程",
            mermaid="sequenceDiagram\n  participant U as User\n  U->>U: RunFlow [Self 42.1%]",
            summary_markdown="# 结果\n\n`RunFlow` 的 Self 样本占比为 42.1%。",
            evidence_artifact_id=str(hiperf["artifact_id"]),
            subpath="",
            agent_run_id="run_test",
        )

        self.assertEqual(len(self.manager.list_results(analysis_kind="hiperf")), 1)
        self.assertEqual(self.manager.list_results(analysis_kind="trace"), [])
        self.assertNotIn("mermaid", self.manager.list_results(analysis_kind="hiperf")[0])
        self.assertIn("RunFlow", self.manager.read_result(str(result["artifact_id"]))["mermaid"])
        self.assertIn("42.1%", self.manager.read_evidence(str(hiperf["artifact_id"]))["text_preview"])

        with self.assertRaises(PlatformError) as raised:
            self.manager.publish_result(
                analysis_kind="hiperf",
                source_connector_id="local_flow_test",
                flow_goal="错误证据类型",
                title="错误结果",
                mermaid="flowchart TD\n  A --> B",
                summary_markdown="证据类型应被拒绝。",
                evidence_artifact_id=str(trace["artifact_id"]),
            )
        self.assertEqual(raised.exception.code, "EXPERIMENTAL_FLOW_EVIDENCE_KIND_MISMATCH")

    def test_router_publishes_a_free_mermaid_result(self) -> None:
        evidence = self.evidence("trace")
        router = PlatformRuntimeActionRouter(
            artifact_store=self.store,
            build_worker=object(),  # type: ignore[arg-type]
            device_gateway=_Gateway(),  # type: ignore[arg-type]
            default_source_connector=lambda: self.connector,
            bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
            experimental_flows=self.manager,
        )
        action_input = {
            "analysis_kind": "trace",
            "source_connector_id": "local_flow_test",
            "flow_goal": "触发并完成操作",
            "title": "Trace 运行流程",
            "mermaid": "sequenceDiagram\n  participant A\n  A->>A: RunFlow 1.2 ms",
            "summary_markdown": "# Trace 说明\n\n跨度为 1.2 ms。",
            "evidence_artifact_id": evidence["artifact_id"],
        }
        run = {
            "run_id": "run_trace",
            "project_id": "local-default",
            "context": {
                "focused_task": {
                    "source_scopes": [
                        {"source_connector_id": "local_flow_test", "subpath": ""},
                        {"source_connector_id": "local_flow_related", "subpath": ""},
                    ]
                }
            },
        }

        router.validate("experimental_flow.publish_result", action_input, run)
        output = router.execute("experimental_flow.publish_result", action_input, run)

        self.assertEqual(output["status"], "succeeded")
        self.assertEqual(output["result"]["analysis_kind"], "trace")
        self.assertEqual(len(output["result"]["source_scopes"]), 2)
        self.assertNotIn("mermaid", output["result"])

    def test_result_preserves_multiple_source_scopes(self) -> None:
        evidence = self.evidence("hiperf")
        result = self.manager.publish_result(
            analysis_kind="hiperf",
            source_connector_id="local_flow_test",
            flow_goal="入口跨仓调用服务",
            title="跨仓函数流程",
            mermaid="sequenceDiagram\n  participant A\n  participant B\n  A->>B: RunService",
            summary_markdown="# 跨仓结果\n\n入口与服务实现位于不同代码仓。",
            evidence_artifact_id=str(evidence["artifact_id"]),
            source_scopes=[
                {"source_connector_id": "local_flow_test", "subpath": ""},
                {"source_connector_id": "local_flow_related", "subpath": ""},
            ],
        )

        scopes = result["source_scopes"]
        self.assertEqual([item["source_connector_id"] for item in scopes], ["local_flow_test", "local_flow_related"])
        self.assertEqual([item["role"] for item in scopes], ["primary", "related"])
        self.assertEqual(result["schema_version"], "experimental_flow_result/v2")
        self.assertEqual(
            len(self.manager.list_results(analysis_kind="hiperf", source_connector_id="local_flow_related")),
            1,
        )

    def test_mermaid_script_directives_are_rejected(self) -> None:
        evidence = self.evidence("hiperf")
        with self.assertRaises(PlatformError) as raised:
            self.manager.publish_result(
                analysis_kind="hiperf",
                source_connector_id="local_flow_test",
                flow_goal="测试不安全 Mermaid",
                title="不安全图",
                mermaid="flowchart TD\n  click A javascript:alert(1)",
                summary_markdown="测试",
                evidence_artifact_id=str(evidence["artifact_id"]),
            )
        self.assertEqual(raised.exception.code, "EXPERIMENTAL_FLOW_MERMAID_UNSAFE")


if __name__ == "__main__":
    unittest.main()
