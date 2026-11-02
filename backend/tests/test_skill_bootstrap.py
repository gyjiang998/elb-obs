from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.agent_runtime import AgentRunStore, AgentRuntime, CredentialLeaseStore
from backend.core import PlatformError
from backend.skill_bootstrap import (
    acknowledge_skill_bootstrap,
    build_skill_bootstrap_requirement,
    merge_skill_bootstrap_requirement,
    observe_skill_bootstrap,
    public_skill_bootstrap,
    validate_skill_bootstrap_ack,
)


class SkillBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        for skill_id, content in {
            "agent-runtime-orchestrator": "# 主规则\n",
            "code-context-retrieval": "# 代码检索\n",
        }.items():
            target = self.workspace / ".agents" / "skills" / skill_id
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(content, encoding="utf-8")
        self.executable = self.workspace / "agent-cli"
        self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.executable.chmod(0o755)
        self.run = {
            "run_id": "run_0123456789abcdef01234567",
            "skill_versions": [
                {"skill_id": "agent-runtime-orchestrator", "version": "2.4.0"},
                {"skill_id": "code-context-retrieval", "version": "1.2.0"},
            ],
            "context": {
                "skill_profile": {
                    "profile_id": "agent-centric-test",
                    "profile_sha256": "profile-sha",
                    "user_skill_revision": 3,
                    "skills": {
                        "agent-runtime-orchestrator": {"version": "2.4.0"},
                    },
                }
            },
        }

    def requirement(self, *, session_id: str = "session-a") -> dict[str, object]:
        return build_skill_bootstrap_requirement(
            workspace=self.workspace,
            run=self.run,
            adapter_id="codeagent",
            adapter_fingerprint="adapter-sha",
            agent_session_id=session_id,
            executable=self.executable,
        )

    @staticmethod
    def ack_input(requirement: dict[str, object]) -> dict[str, object]:
        orchestrator = requirement["orchestrator"]
        assert isinstance(orchestrator, dict)
        return {
            "bootstrap_id": requirement["bootstrap_id"],
            "orchestrator_version": orchestrator["version"],
            "orchestrator_status": "loaded",
            "skill_discovery": [
                {"skill_id": "team-code-analysis", "status": "available", "source": "agent-native:team-code-analysis"},
            ],
        }

    def test_requirement_inventory_and_identity_are_deterministic(self) -> None:
        first = self.requirement()
        second = self.requirement()

        self.assertEqual(first["bootstrap_id"], second["bootstrap_id"])
        self.assertEqual(first["inventory_count"], 2)
        self.assertEqual(first["orchestrator"]["version"], "2.4.0")  # type: ignore[index]
        self.assertEqual(first["target_skills"], {})
        self.assertNotEqual(first["bootstrap_id"], self.requirement(session_id="session-b")["bootstrap_id"])

    def test_pending_acknowledgement_is_observable_but_does_not_block_actions(self) -> None:
        requirement = self.requirement()
        pending, changed = merge_skill_bootstrap_requirement({}, requirement)
        self.assertTrue(changed)
        run = {**self.run, "skill_bootstrap": pending}

        self.assertIsNone(observe_skill_bootstrap(run, "skills.list"))
        diagnostic = observe_skill_bootstrap(run, "reasoning.prepare_hardware_semantic_route")
        self.assertEqual(diagnostic["status"], "pending")  # type: ignore[index]

        acknowledgement = validate_skill_bootstrap_ack(run, self.ack_input(requirement))
        run["skill_bootstrap"] = acknowledge_skill_bootstrap(pending, acknowledgement)
        diagnostic = observe_skill_bootstrap(run, "reasoning.prepare_hardware_semantic_route")
        self.assertEqual(diagnostic["status"], "acknowledged")  # type: ignore[index]
        self.assertEqual(public_skill_bootstrap(run["skill_bootstrap"])["status"], "acknowledged")

        same, changed = merge_skill_bootstrap_requirement(run["skill_bootstrap"], requirement)
        self.assertFalse(changed)
        self.assertEqual(same["status"], "acknowledged")

        replacement, changed = merge_skill_bootstrap_requirement(run["skill_bootstrap"], self.requirement(session_id="session-c"))
        self.assertTrue(changed)
        self.assertEqual(replacement["status"], "pending")

    def test_ack_accepts_observed_skills_and_requires_exact_orchestrator_version(self) -> None:
        requirement = self.requirement()
        pending, _ = merge_skill_bootstrap_requirement({}, requirement)
        run = {**self.run, "skill_bootstrap": pending}
        acknowledgement = validate_skill_bootstrap_ack(run, self.ack_input(requirement))
        self.assertEqual(acknowledgement["skill_discovery"][0]["skill_id"], "team-code-analysis")

        mismatched = self.ack_input(requirement)
        mismatched["orchestrator_version"] = "1.0.0"
        with self.assertRaises(PlatformError) as version_error:
            validate_skill_bootstrap_ack(run, mismatched)
        self.assertEqual(version_error.exception.code, "SKILL_BOOTSTRAP_ORCHESTRATOR_MISMATCH")

    def test_runtime_persists_acknowledgement_as_auditable_run_state(self) -> None:
        requirement = self.requirement()
        pending, _ = merge_skill_bootstrap_requirement({}, requirement)
        acknowledgement = validate_skill_bootstrap_ack(
            {**self.run, "skill_bootstrap": pending},
            self.ack_input(requirement),
        )

        class AckRouter:
            @staticmethod
            def owner(_action_id: str) -> str:
                return "platform"

            @staticmethod
            def validate(_action_id: str, _input_value: dict[str, object], _run: dict[str, object]) -> None:
                return None

            @staticmethod
            def execute(_action_id: str, _input_value: dict[str, object], _run: dict[str, object]) -> dict[str, object]:
                return {"status": "succeeded", "acknowledgement": acknowledgement}

            @staticmethod
            def can_reuse(_action_id: str, _input_value: dict[str, object], _prior: dict[str, object], _run: dict[str, object]) -> bool:
                return True

            @staticmethod
            def cancel(_run: dict[str, object]) -> None:
                return None

        store = AgentRunStore(Path(self.tempdir.name) / "runs")
        runtime = AgentRuntime(
            store=store,
            leases=CredentialLeaseStore(default_ttl_seconds=60),
            model_gateway=lambda *_args, **_kwargs: {},
            action_router=AckRouter(),  # type: ignore[arg-type]
            system_prompt=lambda _run: "unused",
        )
        created = runtime.create(
            project_id="local-default",
            goal="确认 Skill Bootstrap",
            initial_input="等待",
            mode="page_agent",
            context={},
            budget={"max_turns": 1, "max_actions_per_turn": 1, "max_output_tokens": 1024, "context_window_tokens": 8192},
            action_catalog_version="test",
            skill_versions=[],
        )
        run_id = str(created["run_id"])
        store.mutate(
            run_id,
            event_type="test.bootstrap_required",
            event_state="created",
            payload={},
            apply=lambda run: run.update({"skill_bootstrap": pending}),
        )

        result = runtime.execute_external_action(run_id, "platform.ack_skill_bootstrap", self.ack_input(requirement))

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.read(run_id)["skill_bootstrap"]["status"], "acknowledged")
        self.assertTrue(any(event["event_type"] == "skill.bootstrap_acknowledged" for event in store.events(run_id)))


if __name__ == "__main__":
    unittest.main()
