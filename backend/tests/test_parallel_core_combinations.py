"""Physical-core alternatives retain their own objective and feasibility proof."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from backend.core import PlatformError
from backend.parallel_modeling import ParallelModelingManager


class CoreCombinationRecommendationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.manager = ParallelModelingManager(Path(self.temporary.name))
        dataset = self.manager.import_dataset({"builtin_id": "gallery-edit-pmu-20261014"})
        self.analysis = self.manager.analyze({"dataset_id": dataset["id"]})

    def search(self, *, space=None, **payload):
        return self.manager.search({"analysis_id": self.analysis["id"], "space": space or {
            "available_core_group_ids": ["big", "middle"], "worker_counts": [1, 2, 3],
            "frequency_policy": "max_opps"}, **payload})

    def test_latency_keeps_big_plus_two_and_three_middle_cores_as_separate_alternatives(self):
        result = self.search()
        recommendations = result["core_combination_recommendations"]
        self.assertEqual([row["combination_id"] for row in recommendations],
                         ["big1-middle3-small0", "big1-middle2-small0", "big1-middle1-small0"])
        self.assertEqual([row["physical_core_count"] for row in recommendations], [4, 3, 2])
        self.assertEqual([row["candidate_count"] for row in recommendations], [3, 3, 3])
        self.assertEqual(len(result["candidates"]), 9)
        self.assertEqual(recommendations[0]["configuration_index"], result["best"]["configuration_index"])
        self.assertAlmostEqual(recommendations[0]["metrics"]["finish_ms"], 53.87303045981391)
        self.assertAlmostEqual(recommendations[1]["metrics"]["finish_ms"], 57.93173934486719)
        fastest = min(row["metrics"]["finish_ms"] for row in result["candidates"])
        for rank, row in enumerate(recommendations, 1):
            self.assertEqual(row["rank"], rank)
            self.assertTrue(row["feasible"])
            self.assertEqual(row["selection_status"], "objective_optimum")
            self.assertEqual(row["physical_core_counts"]["big"], 1)
            self.assertEqual(row["physical_core_count"], row["core_slots"])
            self.assertAlmostEqual(row["latency_gap_ms"], row["metrics"]["finish_ms"] - fastest)
            self.assertAlmostEqual(row["latency_gap_pct"], 100 * (row["metrics"]["finish_ms"] / fastest - 1))
            self.assertNotIn("timeline", row)
        self.assertIn("timeline", result["best"])

    def test_budget_frequency_is_optimized_independently_for_each_combination(self):
        result = self.search(objective="minimum_frequency_with_budget", budget_ms=86.94, space={
            "available_core_group_ids": ["big", "middle"], "worker_counts": [2, 3], "main_participation": True,
            "frequencies_khz": {"big": [1325000, 1440000, 2500000], "middle": [1076000, 1325000, 2151000]}})
        groups = {row["combination_id"]: row for row in result["core_combination_recommendations"]}
        self.assertEqual(groups["big1-middle3-small0"]["frequency_vector"],
                         {"big": 1325000, "middle": 1076000, "small": None})
        self.assertEqual(groups["big1-middle2-small0"]["frequency_vector"],
                         {"big": 1440000, "middle": 1325000, "small": None})
        self.assertEqual(len(result["candidates"]), 36)
        global_fastest = min(row["metrics"]["finish_ms"] for row in result["candidates"])
        self.assertLess(global_fastest, min(row["metrics"]["finish_ms"] for row in groups.values()))
        for group in groups.values():
            feasible = [row for row in result["candidates"] if row["core_combination_id"] == group["combination_id"] and row["meets_budget"]]
            chosen = min(feasible, key=lambda row: (row["normalized_max_frequency"], row["normalized_frequency_sum"],
                          row["core_slots"], row["metrics"]["finish_ms"], row["configuration_index"]))
            self.assertEqual(group["configuration_index"], chosen["configuration_index"])
            self.assertEqual(group["nominal_feasible_count"], len(feasible))
            self.assertEqual(group["global_fastest_finish_ms"], global_fastest)

    def test_infeasible_group_retains_its_fastest_configuration_and_is_sorted_last(self):
        result = self.search(objective="minimum_frequency_with_budget", budget_ms=60)
        groups = result["core_combination_recommendations"]
        self.assertEqual([row["feasible"] for row in groups], [True, True, False])
        failed = groups[-1]
        self.assertEqual(failed["combination_id"], "big1-middle1-small0")
        self.assertEqual(failed["selection_status"], "budget_infeasible")
        self.assertEqual(failed["recommendation_basis"], "fastest_nominal_fallback")
        self.assertAlmostEqual(failed["metrics"]["finish_ms"], 65.92014266424968)
        self.assertEqual(failed["fastest_configuration_index"], failed["configuration_index"])
        self.assertEqual(failed["nominal_feasible_count"], 0)
        self.assertEqual(failed["frequency_vector"], {"big": 2500000, "middle": 2151000, "small": None})
        self.assertNotEqual(failed["configuration_index"], result["best"]["configuration_index"])

    def test_all_infeasible_preserves_error_contract_with_combination_diagnostics(self):
        with self.assertRaises(PlatformError) as caught:
            self.search(objective="minimum_frequency_with_budget", budget_ms=.01)
        error = caught.exception
        self.assertEqual(error.code, "parallel_modeling_no_feasible_configuration")
        self.assertAlmostEqual(error.details["best_latency_ms"], 53.87303045981391)
        groups = error.details["core_combination_recommendations"]
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(row["selection_status"] == "budget_infeasible" and row["feasible"] is False for row in groups))
        self.assertTrue(all(row["sensitivity_status"] == "not_evaluated" for row in groups))
        self.assertNotIn("candidates", error.details)
        self.assertEqual(self.manager.list_searches(), [])

    def test_same_kind_main_and_workers_count_distinct_physical_cores_without_smt_duplication(self):
        result = self.search(space={"available_core_group_ids": ["middle"], "main_core_group_id": "middle",
            "worker_counts": [0, 1, 2, 3], "main_participation": [False, True],
            "frequencies_khz": {"middle": [1076000, 2151000]}})
        groups = {row["combination_id"]: row for row in result["core_combination_recommendations"]}
        self.assertEqual(set(groups), {"big0-middle1-small0", "big0-middle2-small0", "big0-middle3-small0"})
        self.assertEqual(len(result["candidates"]), 14)
        for count in (1, 2, 3):
            group = groups[f"big0-middle{count}-small0"]
            self.assertEqual(group["physical_core_counts"], {"big": 0, "middle": count, "small": 0})
            self.assertEqual(group["physical_core_count"], count)
            self.assertEqual(group["candidate_count"], 2 if count == 1 else 6)
        for row in result["candidates"]:
            config = row["configuration"]
            self.assertEqual(row["core_combination_id"], f"big0-middle{1 + len(config['workers'])}-small0")
            self.assertEqual({lane["khz"] for lane in [config["main"], *config["workers"]]}, {config["main"]["khz"]})

    def test_robust_budget_proof_is_independent_and_nominally_feasible_group_can_fail(self):
        result = self.search(objective="minimum_frequency_with_budget", budget_ms=86.94, robustness=True, space={
            "available_core_group_ids": ["big", "middle"], "worker_counts": [2, 3], "main_participation": True,
            "frequencies_khz": {"big": [1440000], "middle": [1325000]}})
        groups = result["core_combination_recommendations"]
        self.assertEqual(len(groups), 2)
        passed, failed = groups
        self.assertEqual(passed["combination_id"], "big1-middle3-small0")
        self.assertTrue(passed["feasible"])
        self.assertEqual(passed["recommendation_basis"], "declared_scenarios_objective")
        self.assertEqual(passed["metrics"]["scenario_count"], 18)
        self.assertEqual(passed["sensitivity_status"], "passed_budget")
        self.assertEqual(failed["combination_id"], "big1-middle2-small0")
        self.assertTrue(failed["nominal_feasible"])
        self.assertFalse(failed["feasible"])
        self.assertEqual(failed["selection_status"], "budget_infeasible")
        self.assertEqual(failed["sensitivity_status"], "exceeds_budget")
        self.assertEqual(failed["metrics"]["scenario_count"], 18)
        self.assertGreater(failed["metrics"]["scenario_worst_ms"], 86.94)
        self.assertEqual(result["search_provenance"]["robustness_scenarios_evaluated"],
                         sum(row["metrics"].get("scenario_count", 0) for row in result["candidates"]))

    def test_robust_latency_checks_each_nominal_winner_without_claiming_minimax(self):
        result = self.search(robustness=True)
        groups = result["core_combination_recommendations"]
        self.assertEqual(result["search_provenance"]["robustness_scenarios_evaluated"], 18 * 3)
        self.assertTrue(all(row["metrics"]["scenario_count"] == 18 for row in groups))
        self.assertTrue(all(row["recommendation_basis"] == "nominal_objective" for row in groups))
        self.assertIn("not the minimax optimum", result["search_provenance"]["robustness_meaning"])
        winners = {row["configuration_index"] for row in groups}
        self.assertTrue(all(row["sensitivity_status"] == "not_evaluated" for row in result["candidates"]
                            if row["configuration_index"] not in winners))

    def test_robust_frequency_search_finds_each_groups_own_lowest_verified_point(self):
        result = self.search(objective="minimum_frequency_with_budget", budget_ms=86.94, robustness=True, space={
            "available_core_group_ids": ["big", "middle"], "worker_counts": [2, 3], "main_participation": True,
            "frequencies_khz": {"big": [1325000, 1440000, 2500000], "middle": [1076000, 1325000, 2151000]}})
        groups = {row["combination_id"]: row for row in result["core_combination_recommendations"]}
        self.assertEqual(groups["big1-middle3-small0"]["frequency_vector"],
                         {"big": 1440000, "middle": 1325000, "small": None})
        self.assertEqual(groups["big1-middle2-small0"]["frequency_vector"],
                         {"big": 2500000, "middle": 1076000, "small": None})
        for row in groups.values():
            self.assertTrue(row["feasible"])
            self.assertEqual(row["metrics"]["scenario_count"], 18)
            self.assertLessEqual(row["metrics"]["scenario_worst_ms"], 86.94)
        unchecked = [row for row in result["candidates"] if row["nominal_feasible"]
                     and row["sensitivity_status"] == "not_evaluated"]
        self.assertTrue(unchecked)
        self.assertTrue(all(row["feasible"] is None for row in unchecked))

    def test_all_robust_infeasible_reports_verified_failures_without_a_false_winner(self):
        with self.assertRaises(PlatformError) as caught:
            self.search(objective="minimum_frequency_with_budget", budget_ms=86.94, robustness=True, space={
                "available_core_group_ids": ["big", "middle"], "worker_counts": [2], "main_participation": True,
                "frequencies_khz": {"big": [1440000], "middle": [1325000]}})
        self.assertEqual(caught.exception.code, "parallel_modeling_no_feasible_configuration")
        row = caught.exception.details["core_combination_recommendations"][0]
        self.assertTrue(row["nominal_feasible"])
        self.assertFalse(row["feasible"])
        self.assertEqual(row["sensitivity_status"], "exceeds_budget")
        self.assertEqual(row["recommendation_basis"], "fastest_nominal_fallback")
        self.assertEqual(self.manager.list_searches(), [])

    def test_sensitivity_cancellation_stops_without_persisting_partial_recommendations(self):
        cancel = threading.Event()
        def progress(value):
            if value.get("stage") == "sensitivity":
                cancel.set()
        with self.assertRaises(PlatformError) as caught:
            self.manager.search({"analysis_id": self.analysis["id"], "robustness": True, "space": {
                "available_core_group_ids": ["big", "middle"], "worker_counts": [2, 3], "frequency_policy": "max_opps"}},
                cancel_event=cancel, progress=progress)
        self.assertEqual(caught.exception.code, "job_cancelled")
        self.assertEqual(self.manager.list_searches(), [])


if __name__ == "__main__":
    unittest.main()
