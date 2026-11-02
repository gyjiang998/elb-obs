from __future__ import annotations

import unittest

from backend.core import PlatformError
from backend.page_analysis_workflow import (
    FULL_ANALYSIS_SCOPE,
    page_analysis_completion_correction,
    page_analysis_progress,
    validate_page_analysis_action,
)


def _observation(action_id: str, result: dict[str, object], input_value: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "action_id": action_id,
        "status": "succeeded",
        "input": input_value or {},
        "result": result,
    }


def _failed_observation(action_id: str, code: str) -> dict[str, object]:
    return {
        "action_id": action_id,
        "status": "failed",
        "input": {},
        "error": {"code": code, "message": "failed"},
    }


def _run(*, completed: dict[str, dict[str, object]] | None = None, baseline: list[str] | None = None) -> dict[str, object]:
    return {
        "mode": "page_agent",
        "context": {
            "analysis_record_id": None,
            "page_context": '{"reasoning_lab":{"code_unit":{},"hardware_routing":{},"strategy_results":[]}}',
        },
        "checkpoint": {
            "entry_triage": {
                "execution_scope": FULL_ANALYSIS_SCOPE,
                "analysis_target": "new_input",
                "target_input_id": "input_12345678",
                "source_selection": {
                    "content_mode": "line_range",
                    "line_start": 2,
                    "line_end": 8,
                },
                "input_sha256": "a" * 64,
                "baseline_action_keys": baseline or [],
            },
            "completed_action_keys": completed or {},
        },
    }


class PageAnalysisWorkflowTests(unittest.TestCase):
    def test_full_analysis_starts_with_visible_hotspot_binding(self) -> None:
        run = _run()

        progress = page_analysis_progress(run)

        self.assertEqual(progress["stage"], "hotspot_binding")
        self.assertEqual(progress["required_action"], "assistant.use_pending_text_as_hotspot")
        self.assertEqual(progress["required_action_input"], {
            "input_id": "input_12345678",
            "content_mode": "line_range",
            "line_start": 2,
            "line_end": 8,
        })
        self.assertIn("input_12345678", page_analysis_completion_correction(run) or "")
        with self.assertRaises(PlatformError) as raised:
            validate_page_analysis_action("platform.list_hardware_api_templates", {}, run)
        self.assertEqual(raised.exception.code, "PAGE_ANALYSIS_STAGE_REQUIRED")

    def test_hotspot_binding_must_use_entry_selected_source_range(self) -> None:
        run = _run()

        with self.assertRaises(PlatformError) as raised:
            validate_page_analysis_action(
                "assistant.use_pending_text_as_hotspot",
                {"input_id": "input_12345678", "content_mode": "entire_input"},
                run,
            )

        self.assertEqual(raised.exception.code, "PAGE_ANALYSIS_ACTION_INPUT_MISMATCH")
        validate_page_analysis_action(
            "assistant.use_pending_text_as_hotspot",
            {
                "input_id": "input_12345678",
                "content_mode": "line_range",
                "line_start": 2,
                "line_end": 8,
            },
            run,
        )

    def test_real_action_results_advance_hotspot_route_and_strategy(self) -> None:
        completed = {
            "hotspot": _observation(
                "assistant.use_pending_text_as_hotspot",
                {"analysis_id": "analysis-1", "artifact_id": "art_code"},
            ),
        }
        run = _run(completed=completed)
        initial_route_progress = page_analysis_progress(run)
        self.assertEqual(initial_route_progress["required_action"], "reasoning.run_hardware_semantic_route")
        self.assertIsNone(initial_route_progress.get("recommended_action"))
        with self.assertRaises(PlatformError):
            validate_page_analysis_action("reasoning.run_strategy", {"strategy_id": "hardware_kb"}, run)

        completed["route"] = _observation(
            "reasoning.run_hardware_semantic_route",
            {"routing_result_artifact_id": "art_route", "selected_units": [{"unit_id": "gpu"}]},
        )
        progress = page_analysis_progress(run)
        self.assertEqual(progress["required_action"], "reasoning.run_strategy")
        self.assertEqual(progress["required_strategy_id"], "hardware_kb")
        with self.assertRaises(PlatformError) as raised:
            validate_page_analysis_action("reasoning.run_strategy", {"strategy_id": "direct_baseline"}, run)
        self.assertEqual(raised.exception.code, "PAGE_ANALYSIS_ACTION_INPUT_MISMATCH")

        completed["strategy"] = _observation(
            "reasoning.run_strategy",
            {"result_artifact_id": "art_strategy"},
            {"strategy_id": "hardware_kb"},
        )
        self.assertTrue(page_analysis_progress(run)["ready"])
        validate_page_analysis_action("device.discover", {}, run)

    def test_blind_baseline_is_recommended_only_after_device_execution(self) -> None:
        completed = {
            "hotspot": _observation("assistant.use_pending_text_as_hotspot", {"artifact_id": "art_code"}),
            "route": _observation("reasoning.run_hardware_semantic_route", {"selected_units": [{"unit_id": "gpu"}]}),
            "strategy": _observation("reasoning.run_strategy", {"result_artifact_id": "art_strategy"}, {"strategy_id": "hardware_kb"}),
        }
        run = _run(completed=completed)

        progress = page_analysis_progress(run)

        self.assertTrue(progress["ready"])
        self.assertFalse(progress["device_execution_observed"])
        self.assertIsNone(progress["recommended_action"])
        validate_page_analysis_action("device.discover", {}, run)

        completed["device"] = _observation(
            "device.execute_shell",
            {"remote_shell_run_artifact_id": "art_device_run", "return_code": 0},
        )
        progress = page_analysis_progress(run)
        self.assertTrue(progress["device_execution_observed"])
        self.assertEqual(progress["recommended_action"], "reasoning.run_blind_analysis")

        completed["blind"] = _observation(
            "reasoning.run_blind_analysis",
            {"result_artifact_id": "art_blind"},
        )
        progress = page_analysis_progress(run)
        self.assertEqual(progress["recommended_action"], "reasoning.compare_blind_flow")

    def test_new_route_invalidates_earlier_strategy_until_strategy_is_rerun(self) -> None:
        completed = {
            "hotspot": _observation("assistant.use_pending_text_as_hotspot", {"artifact_id": "art_code"}),
            "route-1": _observation("reasoning.run_hardware_semantic_route", {"routing_result_artifact_id": "art_route_1", "selected_units": [{"unit_id": "gpu"}]}),
            "strategy-1": _observation("reasoning.run_strategy", {"result_artifact_id": "art_strategy_1"}, {"strategy_id": "hardware_kb"}),
            "route-2": _observation("reasoning.run_hardware_semantic_route", {"routing_result_artifact_id": "art_route_2", "selected_units": [{"unit_id": "npu"}]}),
        }
        run = _run(completed=completed)

        progress = page_analysis_progress(run)

        self.assertFalse(progress["ready"])
        self.assertEqual(progress["required_action"], "reasoning.run_strategy")
        completed["strategy-2"] = _observation(
            "reasoning.run_strategy",
            {"result_artifact_id": "art_strategy_2"},
            {"strategy_id": "hardware_kb"},
        )
        self.assertTrue(page_analysis_progress(run)["ready"])

    def test_route_without_hardware_uses_direct_baseline(self) -> None:
        run = _run(completed={
            "hotspot": _observation("assistant.use_pending_text_as_hotspot", {"artifact_id": "art_code"}),
            "route": _observation("reasoning.run_hardware_semantic_route", {"selected_units": []}),
        })

        progress = page_analysis_progress(run)

        self.assertEqual(progress["required_strategy_id"], "direct_baseline")

    def test_ui_timeout_keeps_the_same_page_stage_retryable(self) -> None:
        run = _run(completed={
            "hotspot": _observation("assistant.use_pending_text_as_hotspot", {"artifact_id": "art_code"}),
            "route-timeout": _failed_observation("reasoning.run_hardware_semantic_route", "UI_ACTION_TIMEOUT"),
        })

        progress = page_analysis_progress(run)

        self.assertFalse(progress.get("blocked", False))
        self.assertEqual(progress["stage"], "hardware_routing")
        self.assertEqual(progress["required_action"], "reasoning.run_hardware_semantic_route")
        self.assertIn("reasoning.run_hardware_semantic_route", page_analysis_completion_correction(run) or "")

    def test_non_transient_page_failure_remains_blocked(self) -> None:
        run = _run(completed={
            "hotspot": _observation("assistant.use_pending_text_as_hotspot", {"artifact_id": "art_code"}),
            "route-failure": _failed_observation("reasoning.run_hardware_semantic_route", "INVALID_RESULT"),
        })

        progress = page_analysis_progress(run)

        self.assertTrue(progress["blocked"])
        self.assertEqual(progress["stage"], "hardware_routing_failed")

    def test_request_baseline_prevents_old_session_results_from_satisfying_new_code(self) -> None:
        old = _observation("assistant.use_pending_text_as_hotspot", {"artifact_id": "art_old"})
        run = _run(completed={"old-hotspot": old}, baseline=["old-hotspot"])

        progress = page_analysis_progress(run)

        self.assertEqual(progress["stage"], "hotspot_binding")

    def test_current_page_target_can_continue_existing_visible_artifacts(self) -> None:
        run = _run()
        run["checkpoint"]["entry_triage"]["analysis_target"] = "current_page"  # type: ignore[index]
        run["context"]["page_context"] = (  # type: ignore[index]
            '{"reasoning_lab":{"code_unit":{"artifact_id":"art_code"},'
            '"hardware_routing":{"status":"completed","result_artifact_id":"art_route",'
            '"selected_units":[{"unit_id":"gpu"}]},"strategy_results":['
            '{"strategy_id":"hardware_kb","status":"completed","result_artifact_id":"art_strategy"}]}}'
        )

        self.assertTrue(page_analysis_progress(run)["ready"])


if __name__ == "__main__":
    unittest.main()
