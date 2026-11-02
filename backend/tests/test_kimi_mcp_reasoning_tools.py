from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch


os.environ.setdefault("HETERO_AGENT_RUN_ID", "run_reasoning_tools_test")
os.environ.setdefault("HETERO_AGENT_TOOL_TOKEN", "reasoning-tools-test-token")

from backend import kimi_mcp_server  # noqa: E402


class KimiMcpReasoningToolTests(unittest.TestCase):
    def test_fastmcp_catalog_contains_application_flow_compiler(self) -> None:
        names = [tool.name for tool in asyncio.run(kimi_mcp_server.mcp.list_tools())]

        self.assertEqual(names[0], "platform_call_tool")
        self.assertIn("application_flow_compile_mermaid", names)
        self.assertIn("application_flow_enrich_trace_anchors", names)
        self.assertIn("application_flow_inspect_trace_instrumentation", names)
        self.assertIn("application_flow_inspect_runtime_trace", names)
        self.assertIn("application_flow_apply_runtime_trace_alignment", names)
        self.assertIn("experimental_flow_read_evidence", names)
        self.assertIn("experimental_flow_publish_result", names)
        self.assertIn("platform_list_hardware_api_templates", names)
        self.assertIn("platform_read_hardware_api_template", names)
        self.assertIn("reasoning_prepare_context", names)
        self.assertEqual(len(names), len(set(names)))

    def test_experimental_flow_publish_tool_forwards_free_mermaid(self) -> None:
        expected = {"status": "succeeded", "result": {"artifact_id": "art_result"}}
        with patch.object(kimi_mcp_server, "_action", return_value=expected) as action:
            output = kimi_mcp_server.experimental_flow_publish_result(
                "hiperf",
                "local_source",
                "打开页面",
                "函数调用流程",
                "sequenceDiagram\n  A->>B: Run [Self 22%]",
                "# 采样说明",
                "art_1234567890abcdef12345678",
                source_scopes=[
                    kimi_mcp_server.ExperimentalFlowSourceScope(
                        source_connector_id="local_source",
                        source_snapshot_id="source_snapshot_1",
                        subpath="module",
                    )
                ],
            )

        self.assertEqual(output, expected)
        self.assertEqual(action.call_args.args[0], "experimental_flow.publish_result")
        self.assertEqual(action.call_args.args[1]["analysis_kind"], "hiperf")
        self.assertIn("sequenceDiagram", action.call_args.args[1]["mermaid"])
        self.assertEqual(action.call_args.args[1]["source_scopes"][0]["subpath"], "module")

    def test_generic_tool_entry_calls_the_registered_typed_tool(self) -> None:
        expected = {"status": "succeeded", "result": {"template_count": 9}}

        with patch.object(kimi_mcp_server, "_action", return_value=expected) as action:
            asyncio.run(
                kimi_mcp_server.mcp.call_tool(
                    "platform_call_tool",
                    {
                        "tool_name": "platform_list_hardware_api_templates",
                        "input": {"unit": "gpu", "query": "逐像素图像计算"},
                    },
                )
            )

        self.assertEqual(
            action.call_args.args,
            (
                "platform.list_hardware_api_templates",
                {"unit": "gpu", "profile_id": "", "api_family": "", "query": "逐像素图像计算"},
            ),
        )

    def test_generic_tool_entry_rejects_unknown_or_recursive_targets(self) -> None:
        for tool_name in ("missing_tool", "platform_call_tool"):
            with self.subTest(tool_name=tool_name):
                with self.assertRaises(Exception):
                    asyncio.run(
                        kimi_mcp_server.mcp.call_tool(
                            "platform_call_tool",
                            {"tool_name": tool_name, "input": {}},
                        )
                    )

    def test_fastmcp_catalog_uses_portable_input_schemas(self) -> None:
        tools = asyncio.run(kimi_mcp_server.mcp.list_tools())

        for tool in tools:
            serialized = json.dumps(tool.inputSchema, ensure_ascii=True)
            for unsupported_keyword in ("$defs", "$ref", "anyOf", "oneOf", "allOf"):
                self.assertNotIn(f'"{unsupported_keyword}"', serialized, tool.name)

        enrich = next(tool for tool in tools if tool.name == "application_flow_enrich_trace_anchors")
        stage_item = enrich.inputSchema["properties"]["stage_results"]["items"]
        self.assertEqual(stage_item["type"], "object")
        self.assertEqual(stage_item["properties"]["start"]["type"], "object")
        self.assertNotIn("start", stage_item["required"])

        alignment = next(tool for tool in tools if tool.name == "application_flow_apply_runtime_trace_alignment")
        match_item = alignment.inputSchema["properties"]["stage_matches"]["items"]
        self.assertEqual(match_item["type"], "object")
        self.assertIn("stage_id", match_item["required"])
        self.assertIn("match_status", match_item["required"])
        node_match_item = alignment.inputSchema["properties"]["node_matches"]["items"]
        self.assertEqual(node_match_item["type"], "object")
        self.assertIn("node_id", node_match_item["required"])

    def test_trace_anchor_tool_keeps_typed_execution_validation(self) -> None:
        expected = {"status": "succeeded"}
        arguments = {
            "source_connector_id": "local_source",
            "discovery_artifact_id": "art_discovery",
            "scenario_artifact_id": "art_scenario",
            "flow_id": "camera_start",
            "stage_results": [
                {
                    "stage_id": "stage_1",
                    "start": {
                        "event_name": "CameraStart",
                        "path": "src/camera.cpp",
                        "line": 42,
                        "macro": "HITRACE_METER_NAME",
                        "symbol": "Camera::Start",
                    },
                    "notes": "源码已核对",
                }
            ],
        }

        with patch.object(kimi_mcp_server, "_action", return_value=expected) as action:
            asyncio.run(
                kimi_mcp_server.mcp.call_tool(
                    "application_flow_enrich_trace_anchors",
                    arguments,
                )
            )

        payload = action.call_args.args[1]
        self.assertEqual(payload["stage_results"][0]["start"]["event_name"], "CameraStart")
        self.assertNotIn("end", payload["stage_results"][0])
        self.assertEqual(payload["stage_results"][0]["start"]["event_label_match"], "exact_literal")

        invalid_arguments = {
            **arguments,
            "stage_results": [
                {
                    **arguments["stage_results"][0],
                    "start": {**arguments["stage_results"][0]["start"], "line": 0},
                }
            ],
        }
        with patch.object(kimi_mcp_server, "_action") as action:
            with self.assertRaises(Exception):
                asyncio.run(
                    kimi_mcp_server.mcp.call_tool(
                        "application_flow_enrich_trace_anchors",
                        invalid_arguments,
                    )
                )
        action.assert_not_called()

    def test_skill_bootstrap_ack_forwards_structured_discovery(self) -> None:
        expected = {"status": "succeeded"}
        discovery = [
            kimi_mcp_server.SkillBootstrapDiscovery(skill_id="team-code-analysis", status="available", source="skill-b"),
        ]
        with patch.object(kimi_mcp_server, "_action", return_value=expected) as action:
            result = kimi_mcp_server.platform_ack_skill_bootstrap(
                "bootstrap-sha",
                "2.2.0",
                discovery,
                agent_note="已按当前 Agent 的真实目录完成发现。",
            )

        self.assertEqual(result, expected)
        self.assertEqual(action.call_args.args[0], "platform.ack_skill_bootstrap")
        self.assertEqual(action.call_args.args[1]["skill_discovery"][0]["status"], "available")

    def test_direct_hardware_route_tools_forward_typed_actions(self) -> None:
        prepared = {"status": "prepared", "context_artifact_id": "art_context"}
        saved = {"status": "succeeded", "routing_result_artifact_id": "art_route"}

        with patch.object(kimi_mcp_server, "_action", side_effect=[prepared, saved]) as action:
            self.assertEqual(kimi_mcp_server.reasoning_prepare_hardware_semantic_route(), prepared)
            self.assertEqual(
                kimi_mcp_server.reasoning_run_hardware_semantic_route(
                    "art_context",
                    {"schema_version": "hardware_semantic_routing/v2"},
                    reason="new evidence",
                    retry_nonce="retry-1",
                ),
                saved,
            )

        self.assertEqual(
            action.call_args_list[0].args,
            ("reasoning.prepare_hardware_semantic_route", {}),
        )
        self.assertEqual(
            action.call_args_list[1].args,
            (
                "reasoning.run_hardware_semantic_route",
                {
                    "context_artifact_id": "art_context",
                    "agent_result": {"schema_version": "hardware_semantic_routing/v2"},
                    "reason": "new evidence",
                    "retry_nonce": "retry-1",
                },
            ),
        )

    def test_direct_strategy_tools_forward_typed_actions(self) -> None:
        prepared = {"status": "prepared", "context_artifact_id": "art_strategy_context"}
        saved = {"status": "succeeded", "result_artifact_id": "art_strategy"}

        with patch.object(kimi_mcp_server, "_action", side_effect=[prepared, saved]) as action:
            self.assertEqual(kimi_mcp_server.reasoning_prepare_context("hardware_kb"), prepared)
            self.assertEqual(
                kimi_mcp_server.reasoning_run_strategy(
                    "hardware_kb",
                    "art_strategy_context",
                    {"schema_version": "opportunity_reasoning/v1"},
                ),
                saved,
            )

        self.assertEqual(
            action.call_args_list[0].args,
            ("reasoning.prepare_context", {"strategy_id": "hardware_kb"}),
        )
        self.assertEqual(
            action.call_args_list[1].args,
            (
                "reasoning.run_strategy",
                {
                    "strategy_id": "hardware_kb",
                    "context_artifact_id": "art_strategy_context",
                    "agent_result": {"schema_version": "opportunity_reasoning/v1"},
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
