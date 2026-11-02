from __future__ import annotations

import unittest

from backend import app as platform_app
from backend.skills import (
    SKILL_EVOLUTION_CONTRACT_VERSION,
    SKILL_EVOLUTION_EVENTS,
    SKILL_MANIFESTS,
    skill_catalog,
    validate_skill_registry,
)


class SkillGovernanceTests(unittest.TestCase):
    def test_every_registered_backend_skill_has_a_complete_governance_profile(self) -> None:
        self.assertEqual(validate_skill_registry(), [])

        catalog = skill_catalog()
        self.assertEqual(len(catalog), len(SKILL_MANIFESTS))
        self.assertEqual({item["skill_id"] for item in catalog}, {item["skill_id"] for item in SKILL_MANIFESTS})

        for skill in catalog:
            with self.subTest(skill_id=skill["skill_id"]):
                self.assertEqual(skill["skill_contract_version"], SKILL_EVOLUTION_CONTRACT_VERSION)
                self.assertTrue(skill["skill_class"])
                self.assertTrue(skill["claim_boundary"])
                self.assertGreaterEqual(len(skill["required_evidence"]), 1)
                self.assertGreaterEqual(len(skill["quality_gates"]), 1)
                self.assertTrue(skill["evaluation"]["golden_fixture"])
                self.assertGreaterEqual(len(skill["evaluation"]["semantic_assertions"]), 1)
                self.assertGreaterEqual(len(skill["evaluation"]["failure_scenarios"]), 1)
                self.assertFalse(skill["change_control"]["automatic_release"])

    def test_catalog_is_materialized_without_mutating_the_execution_manifest(self) -> None:
        raw = next(item for item in SKILL_MANIFESTS if item["skill_id"] == "trace-hotspot-discovery")
        self.assertNotIn("skill_contract_version", raw)

        materialized = next(item for item in skill_catalog() if item["skill_id"] == raw["skill_id"])
        materialized["quality_gates"][0]["requirement"] = "mutated by test"

        fresh = next(item for item in skill_catalog() if item["skill_id"] == raw["skill_id"])
        self.assertNotEqual(fresh["quality_gates"][0]["requirement"], "mutated by test")
        self.assertNotIn("skill_contract_version", raw)

    def test_runtime_action_catalog_exposes_the_governed_skill_registry(self) -> None:
        response = platform_app.page_actions()
        direct = next(item for item in response["skills"] if item["skill_id"] == "selected-event-opportunity-identification")

        self.assertIn("精确", direct["claim_boundary"])
        self.assertTrue(any(gate["gate_id"] == "exact_binding" for gate in direct["quality_gates"]))
        self.assertEqual(direct["evaluation"]["golden_fixture"], "captured-report-json")

    def test_agent_owned_reasoning_actions_require_exact_context_and_result(self) -> None:
        actions = {item["action_id"]: item for item in platform_app.page_actions()["actions"]}

        strategy_required = set(actions["reasoning.run_strategy"]["input_schema"]["required"])
        route_required = set(actions["reasoning.run_hardware_semantic_route"]["input_schema"]["required"])

        self.assertEqual(strategy_required, {"strategy_id", "context_artifact_id", "agent_result"})
        self.assertEqual(route_required, {"context_artifact_id", "agent_result"})
        self.assertIn("never calls a model", actions["reasoning.prepare_context"]["implementation_ref"])
        owned_refs = "\n".join(
            actions[action_id]["implementation_ref"]
            for action_id in (
                "reasoning.prepare_context",
                "reasoning.run_strategy",
                "reasoning.prepare_hardware_semantic_route",
                "reasoning.run_hardware_semantic_route",
            )
        )
        self.assertIn("exact context artifact", owned_refs)
        for phrase in ("Agent Session", "Kimi Session", "same Session", "do not delegate"):
            self.assertNotIn(phrase, owned_refs)

    def test_published_evolution_events_include_the_ipc_calibration_regression(self) -> None:
        event = SKILL_EVOLUTION_EVENTS["direct-ipc-evidence-calibration-20260728"]

        self.assertEqual(event["problem_class"], "evidence_miscalibration")
        self.assertIn("candidate", event["regression_assertion"])

    def test_device_validation_skill_governs_runner_and_curated_templates(self) -> None:
        device_skill = next(
            item for item in skill_catalog() if item["skill_id"] == "agent-native-device-experimentation"
        )
        gate_ids = {gate["gate_id"] for gate in device_skill["quality_gates"]}

        self.assertEqual(device_skill["version"], "3.0.0")
        self.assertIn("official_test_layer_selection", gate_ids)
        self.assertIn("runner_execution_truth", gate_ids)
        self.assertIn("curated_template_provenance", gate_ids)
        self.assertIn("template_semantic_adaptation", gate_ids)
        self.assertIn("template_evidence_separation", gate_ids)
        self.assertIn("official-test-framework-selection-20260805", device_skill["evolution"]["event_ids"])
        self.assertIn("curated-api-template-library-20260805", device_skill["evolution"]["event_ids"])
        self.assertTrue(any("standalone fallback" in assertion for assertion in device_skill["evaluation"]["semantic_assertions"]))
        provenance_gate = next(gate for gate in device_skill["quality_gates"] if gate["gate_id"] == "curated_template_provenance")
        self.assertIn("SHA-256", provenance_gate["requirement"])
        self.assertTrue(any("原样提交模板" in scenario for scenario in device_skill["evaluation"]["failure_scenarios"]))
