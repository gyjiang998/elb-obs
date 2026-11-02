from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.analysis_execution import AnalysisWorkspaceStore, build_analysis_execution_view


class _TraceStore:
    def read(self, analysis_id: str, *, limit: int) -> dict:
        return {
            "trace_id": f"trace:{analysis_id}",
            "relative_path": f"analysis-traces/{analysis_id}.jsonl",
            "events": [{
                "event_id": "page-1",
                "occurred_at": "2026-08-28T01:00:00Z",
                "category": "workflow",
                "operation": "analysis.hotspot.update",
                "status": "succeeded",
                "payload": {"event_name": "hotspot"},
            }],
        }


class _SessionRegistry:
    def resolve(self, analysis_id: str) -> str:
        return "run-1"


class _RunStore:
    run = {
        "run_id": "run-1",
        "context": {"runtime_environment": {"agent_adapter_id": "kimi-cli"}},
        "engine": {"workspace_id": "wire-workspace", "session_id": "native-1"},
    }

    def read(self, run_id: str) -> dict:
        return self.run

    def events(self, run_id: str, *, after_sequence: int, limit: int) -> list[dict]:
        return [{
            "event_id": "run-event-1",
            "run_id": run_id,
            "occurred_at": "2026-08-28T01:00:01Z",
            "event_type": "tool.result",
            "state": "succeeded",
            "payload": {"tool": "reasoning.prepare"},
        }]

    def public(self, run: dict) -> dict:
        return {"run_id": run["run_id"], "state": "running"}


class AnalysisExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.workspace_store = AnalysisWorkspaceStore(self.root / "workspaces")

    def _write_native_wire(self) -> None:
        wire = self.root / "kimi" / "sessions" / "wire-workspace" / "session-1" / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"type": "profile.bind", "agentId": "main", "systemPrompt": "system contract"},
            {"type": "turn.prompt", "agentId": "main", "input": "analyze hotspot"},
            {"type": "context.append_loop_event", "agentId": "main", "time": 1787880001000, "event": {"type": "step.begin", "step": 1, "uuid": "step-1"}},
            {"type": "llm.request", "agentId": "main", "turnStep": "0.1", "modelAlias": "K2.7", "api_key": "secret"},
            {"type": "context.append_loop_event", "agentId": "main", "time": 1787880001100, "event": {"type": "content.part", "part": {"type": "think", "think": "compare data shape"}}},
            {"type": "context.append_loop_event", "agentId": "main", "time": 1787880001200, "event": {"type": "tool.call", "toolCallId": "tool-1", "name": "mcp__platform__reasoning_prepare", "args": {"api_key": "secret", "unit": "gpu"}}},
            {"type": "context.append_loop_event", "agentId": "main", "time": 1787880001300, "event": {"type": "tool.result", "toolCallId": "tool-1", "result": {"status": "succeeded", "artifact_id": "art-1"}}},
            {"type": "context.append_loop_event", "agentId": "main", "time": 1787880001400, "event": {"type": "content.part", "part": {"type": "text", "text": "GPU candidate"}}},
            {"type": "context.append_loop_event", "agentId": "main", "time": 1787880001500, "event": {"type": "step.end", "finishReason": "stop", "usage": {"inputOther": 100, "inputCacheRead": 40, "inputCacheCreation": 10, "output": 20}, "llmFirstTokenLatencyMs": 75}},
        ]
        wire.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def test_workspace_snapshot_is_versioned_and_redacted(self) -> None:
        first = self.workspace_store.write("analysis-1", {"stage": "route_hardware", "api_key": "never-store"})
        second = self.workspace_store.write("analysis-1", {"stage": "complete", "nested": {"authorization": "Bearer token"}})

        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(second["snapshot"]["nested"]["authorization"], "[已脱敏]")
        self.assertNotIn("Bearer token", (self.root / "workspaces" / "analysis-1.json").read_text(encoding="utf-8"))

    def test_workspace_catalog_returns_only_valid_analysis_records(self) -> None:
        self.workspace_store.write("analysis-1", {
            "analysis_record": {"analysisId": "analysis-1", "updatedAt": "2026-08-28T01:00:00Z"},
            "workflow": {"stage": "complete"},
        })
        self.workspace_store.write("analysis-2", {"analysis_record": {"analysisId": "wrong-id"}})

        catalog = self.workspace_store.catalog()

        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["analysis_id"], "analysis-1")
        self.assertEqual(catalog[0]["workflow_stage"], "complete")

    def test_execution_view_merges_browser_run_and_native_model_trace(self) -> None:
        self.workspace_store.write("analysis-1", {"workflow": {"stage": "complete"}})
        self._write_native_wire()
        with patch.dict(os.environ, {"KIMI_CODE_HOME": str(self.root / "kimi")}):
            result = build_analysis_execution_view(
                "analysis-1",
                trace_store=_TraceStore(),
                session_registry=_SessionRegistry(),
                run_store=_RunStore(),
                workspace_store=self.workspace_store,
            )

        self.assertTrue(result["coverage"]["workspace_snapshot"])
        self.assertTrue(result["coverage"]["browser_events"])
        self.assertTrue(result["coverage"]["agent_run"])
        self.assertTrue(result["coverage"]["native_model_transcript"])
        self.assertEqual(result["summary"]["model_step_count"], 1)
        self.assertEqual(result["summary"]["tool_call_count"], 1)
        self.assertEqual(result["summary"]["tool_success_count"], 1)
        self.assertEqual(result["summary"]["input_tokens"], 150)
        self.assertEqual(result["summary"]["output_tokens"], 20)
        self.assertAlmostEqual(result["summary"]["cache_hit_rate"], 40 / 150)
        self.assertEqual(result["summary"]["duration_ms"], 500)
        self.assertEqual(result["summary"]["record_span_ms"], 1_201_000)
        self.assertEqual([event["source"] for event in result["events"]], ["browser", "agent_run"])
        step = result["model_transcript"]["steps"][0]
        self.assertEqual(step["input"], "analyze hotspot")
        self.assertEqual(step["reasoning"], "compare data shape")
        self.assertEqual(step["output"], "GPU candidate")
        self.assertEqual(step["model_request"]["modelAlias"], "K2.7")
        self.assertEqual(step["model_request"]["api_key"], "[已脱敏]")
        self.assertEqual(step["tools"][0]["arguments"]["api_key"], "[已脱敏]")


if __name__ == "__main__":
    unittest.main()
