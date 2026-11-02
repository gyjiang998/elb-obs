from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.agent_runtime import AgentRunStore
from backend.analysis_observability import AnalysisTraceStore
from backend.core import ArtifactStore, PlatformError
from backend.experience_evolution import ExperienceEvolutionManager


class ExperienceEvolutionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ArtifactStore(self.root / "state", project_id="local-default")
        self.run_store = AgentRunStore(self.root / "state" / "agent-runs")
        self.trace_store = AnalysisTraceStore(self.root / "state" / "analysis-traces")
        self.manager = ExperienceEvolutionManager(
            self.root / "state" / "experience-evolution",
            artifact_store=self.store,
            analysis_trace_store=self.trace_store,
            agent_run_store=self.run_store,
        )
        self.run = self.run_store.create(
            project_id="local-default",
            goal="验证 GPU 真机实验",
            initial_input="开始实验",
            mode="device_validation",
            context={
                "runtime_environment": {
                    "agent_adapter_id": "codeagent",
                    "agent_display_name": "CodeAgent",
                    "model_name": "minimax-m2.7",
                    "host_os": "Linux",
                    "host_architecture": "x86_64",
                },
                "target_platform_profile": {
                    "chipset": {"model_label": "Mate 80 Pro"},
                    "operating_system": {"version_label": "HarmonyOS"},
                },
                "skill_profile": {"profile_id": "agent-centric"},
            },
            budget={},
            action_catalog_version="test",
            skill_versions=[],
        )
        self.trace_store.append(
            "analysis-test",
            {
                "event_id": "evt-1",
                "category": "tool_error",
                "operation": "build.create",
                "status": "failed",
                "payload": {"code": "BUILD_LINK_FAILED", "message": "linker thread creation failed"},
            },
        )

    def _review(self) -> dict[str, object]:
        run_id = str(self.run["run_id"])
        return {
            "title": "跨环境构建失败复盘",
            "summary_zh": "工具事实显示链接阶段失败，模型与 Agent 差异仍需对照。",
            "source_refs": [
                {"source_type": "agent_run", "source_id": run_id},
                {"source_type": "analysis_trace", "source_id": "analysis-test"},
            ],
            "environment_comparison": [
                {"dimension": "Agent 框架", "baseline": "Kimi CLI", "observed": "CodeAgent", "impact": "工具发现路径可能不同"},
            ],
            "pitfalls": [
                {
                    "recurrence_key": "build.linker.thread-resource-exhaustion",
                    "title": "链接器线程资源不足",
                    "category": "host_environment",
                    "symptom": "编译成功但链接器创建线程失败。",
                    "failure_stage": "build.create",
                    "root_cause_hypothesis": "Build Worker 的进程或线程资源达到上限。",
                    "confidence": "medium",
                    "trigger_conditions": ["链接阶段", "宿主资源受限"],
                    "evidence_source_ids": [run_id, "analysis-test"],
                    "applicability_scope": "使用同一 Build Worker 资源配额的链接任务。",
                    "generalization_boundary": "不能据此断言所有链接失败均由线程上限导致。",
                    "affected_skill_ids": ["agent-native-device-experimentation"],
                    "resolution_options": [
                        {
                            "resolution_id": "limit-linker-threads",
                            "kind": "environment_config",
                            "title": "降低链接器并行度",
                            "rationale": "先用更小线程数验证资源上限假设。",
                            "operator_steps": ["检查进程和线程配额"],
                            "agent_steps": ["使用固定低并行度重新链接"],
                            "prerequisites": ["允许调整 Build Worker 环境"],
                            "risks": ["构建时间增加"],
                            "verification": "相同输入重复链接三次且不再出现线程创建错误。",
                            "target_skill_ids": [],
                            "recommended": True,
                        }
                    ],
                }
            ],
        }

    def test_catalog_exposes_environment_identity_and_sources(self) -> None:
        catalog = self.manager.source_catalog()
        run_source = next(item for item in catalog["sources"] if item["source_type"] == "agent_run")

        self.assertEqual(run_source["environment"]["model_name"], "minimax-m2.7")
        self.assertEqual(run_source["environment"]["host_os"], "Linux")
        self.assertTrue(any(item["source_id"] == "analysis-test" for item in catalog["sources"]))

        detail = self.manager.read_source("agent_run", str(self.run["run_id"]))
        self.assertEqual(detail["environment"]["model_name"], "minimax-m2.7")
        self.assertEqual(detail["environment"]["agent_display_name"], "CodeAgent")

    def test_review_deduplicates_problem_class_and_triage_is_a_separate_artifact(self) -> None:
        first = self.manager.save_review(self._review(), agent_run_id=str(self.run["run_id"]))
        second = self.manager.save_review(self._review(), agent_run_id=str(self.run["run_id"]))
        catalog = self.manager.catalog()

        self.assertEqual(first["review_id"], second["review_id"])
        self.assertEqual(catalog["case_count"], 1)
        self.assertEqual(catalog["review_count"], 1)
        case = catalog["cases"][0]
        decision = self.manager.triage_case(
            case["case_id"],
            status="action_planned",
            selected_resolution_id="limit-linker-threads",
            operator_note="先在 Linux worker 复验",
        )

        self.assertEqual(decision["case"]["status"], "action_planned")
        self.assertTrue(decision["decision_artifact_id"].startswith("art_"))
        self.assertEqual(len(self.store.list_artifacts("execution_experience_review")), 1)
        self.assertEqual(len(self.store.list_artifacts("experience_triage_decision")), 1)

    def test_source_catalog_orders_sessions_by_creation_not_last_activity(self) -> None:
        older_active = {
            "run_id": "run_111111111111111111111111",
            "goal": "较早但仍活跃的 Session",
            "created_at": "2026-08-27T01:00:00Z",
            "updated_at": "2026-08-27T09:00:00Z",
            "state": "running",
            "state_detail": "仍在轮询",
            "event_cursor": 20,
            "engine": {},
            "context": {},
        }
        newer_finished = {
            "run_id": "run_222222222222222222222222",
            "goal": "较新且已结束的 Session",
            "created_at": "2026-08-27T02:00:00Z",
            "updated_at": "2026-08-27T03:00:00Z",
            "state": "completed",
            "state_detail": "已完成",
            "event_cursor": 8,
            "engine": {},
            "context": {},
        }
        with (
            patch.object(self.run_store, "list_recent", return_value=[older_active, newer_finished]) as list_recent,
            patch.object(self.trace_store, "catalog", return_value=[]),
            patch.object(self.store, "list_artifacts", return_value=[]),
        ):
            sources = self.manager.source_catalog()["sources"]

        self.assertEqual([item["source_id"] for item in sources], [newer_finished["run_id"], older_active["run_id"]])
        list_recent.assert_called_once_with(limit=500, order_by="created_at")

    def test_review_rejects_missing_historical_source(self) -> None:
        review = self._review()
        review["source_refs"] = [{"source_type": "agent_run", "source_id": "run_000000000000000000000000"}]
        review["pitfalls"][0]["evidence_source_ids"] = ["run_000000000000000000000000"]  # type: ignore[index]

        with self.assertRaises(PlatformError):
            self.manager.save_review(review)
