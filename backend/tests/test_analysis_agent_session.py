from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.analysis_agent_session import AnalysisAgentSessionRegistry, normalize_analysis_id
from backend.agent_runtime import AgentRunStore
from backend.core import PlatformError


class AnalysisAgentSessionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.registry = AnalysisAgentSessionRegistry(self.root / "analysis-sessions.json")

    def test_binding_survives_registry_reopen(self) -> None:
        analysis_id = "c52f812e-8aa4-4bf7-853d-b65fcce84c6a"
        run_id = "run_" + "a" * 24

        self.registry.bind(analysis_id, run_id)
        reopened = AnalysisAgentSessionRegistry(self.root / "analysis-sessions.json")

        self.assertEqual(reopened.resolve(analysis_id), run_id)
        self.assertEqual(reopened.analysis_id_for_run(run_id), analysis_id)

    def test_analysis_cannot_silently_rebind_to_another_run(self) -> None:
        analysis_id = "analysis-one"
        self.registry.bind(analysis_id, "run_" + "a" * 24)

        with self.assertRaises(PlatformError) as raised:
            self.registry.bind(analysis_id, "run_" + "b" * 24)

        self.assertEqual(raised.exception.code, "ANALYSIS_SESSION_ALREADY_BOUND")

    def test_run_cannot_silently_bind_to_multiple_analyses(self) -> None:
        run_id = "run_" + "a" * 24
        self.registry.bind("analysis-one", run_id)

        with self.assertRaises(PlatformError) as raised:
            self.registry.bind("analysis-two", run_id)

        self.assertEqual(raised.exception.code, "AGENT_RUN_ALREADY_BOUND")
        self.assertEqual(raised.exception.details["analysis_id"], "analysis-one")

    def test_run_store_can_migrate_pre_index_analysis_run(self) -> None:
        store = AgentRunStore(self.root / "runs")
        created = store.create(
            project_id="local-default",
            goal="旧版分析任务",
            initial_input="继续任务",
            mode="page_agent",
            context={"analysis_record_id": "analysis-migration"},
            budget={"max_turns": 2, "max_actions_per_turn": 2, "max_output_tokens": 1024, "context_window_tokens": 8192},
            action_catalog_version="test",
            skill_versions=[],
        )

        migrated = store.find_latest_by_analysis_id("analysis-migration")

        self.assertIsNotNone(migrated)
        self.assertEqual(migrated["run_id"], created["run_id"])

    def test_invalid_analysis_id_is_rejected(self) -> None:
        with self.assertRaises(PlatformError):
            normalize_analysis_id("../../outside")


if __name__ == "__main__":
    unittest.main()
