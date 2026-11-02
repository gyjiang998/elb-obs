from __future__ import annotations

import json
import unittest
from copy import deepcopy

from backend import app as platform_app
from backend.hardware_strategy_evaluation import (
    build_reference_result,
    case_by_id,
    load_hardware_strategy_suite,
    public_score_result,
    score_case_result,
    suite_public_catalog,
    summarize_case_scores,
)


class HardwareStrategyEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = load_hardware_strategy_suite()
        cls.profile = platform_app._default_target_platform_profile()

    def _context(self, case_id: str) -> tuple[dict[str, object], dict[str, object]]:
        case = case_by_id(self.suite, case_id)
        context = platform_app._hardware_strategy_evaluation_context(case, self.profile)
        return case, context

    def test_suite_has_stratified_cases_and_public_catalog_never_exposes_oracles(self) -> None:
        catalog = suite_public_catalog(self.suite)

        self.assertEqual(catalog["suite_id"], "openharmony_hardware_kb_context_skill")
        self.assertEqual(catalog["suite_version"], "2.0.0")
        self.assertEqual(catalog["case_count"], 60)
        self.assertEqual(len(catalog["cases"]), 60)
        self.assertEqual(catalog["partitions"], {"development": 16, "holdout": 10, "smoke": 20, "validation": 14})
        self.assertNotIn("oracle", json.dumps(catalog, ensure_ascii=False))
        self.assertEqual(catalog["strategy_contract"]["skill_id"], "strategy-hardware-kb")
        self.assertEqual(catalog["strategy_contract"]["skill_version"], "1.7.0")
        self.assertEqual(catalog["strategy_contract"]["prompt_contract_version"], "opportunity-reasoning-prompt/v8")
        self.assertIn("diagnostic_metric_zh", catalog["evaluation_policy"])
        self.assertTrue(all(item["partition"] in catalog["partitions"] for item in catalog["cases"]))
        self.assertIn("code", catalog["cases"][0]["public_input"])

    def test_every_fixture_preflights_against_current_source_derived_kbs(self) -> None:
        failed: list[str] = []
        for case in self.suite["cases"]:
            with self.subTest(case_id=case["case_id"]):
                context = platform_app._hardware_strategy_evaluation_context(case, self.profile)
                self.assertNotIn("oracle", json.dumps(context, ensure_ascii=False))
                score = score_case_result(case, context, build_reference_result(case, context))
                failed.extend(
                    f"{case['case_id']}:{check['check_id']}"
                    for check in score["checks"]
                    if check["check_id"] == "context_preflight" and not check["passed"]
                )
        self.assertEqual(failed, [])

    def test_reference_self_check_scores_all_cases_without_claiming_device_support(self) -> None:
        scores = []
        for case in self.suite["cases"]:
            context = platform_app._hardware_strategy_evaluation_context(case, self.profile)
            result = build_reference_result(case, context)
            score = score_case_result(case, context, result)
            scores.append(score)
            self.assertTrue(score["passed"], case["case_id"])
            self.assertEqual(score["score"], 100.0)
            self.assertEqual(score["validated_result"]["recommendation"], "evidence_required")
        report = summarize_case_scores(self.suite, scores)
        self.assertEqual(report["score"]["average"], 100.0)
        self.assertEqual(report["score"]["passed_case_count"], 60)
        self.assertEqual(report["quality_summary"]["hardware"]["balanced_accuracy"], 1.0)
        self.assertEqual(report["quality_summary"]["cpu_parallel"]["balanced_accuracy"], 1.0)
        self.assertEqual(report["quality_summary"]["quality_dimensions"]["scope_disclosure_rate"], 1.0)
        self.assertEqual(report["partition_metrics"]["holdout"]["case_count"], 10)

    def test_undeclared_scope_is_a_diagnostic_failure_without_changing_the_evidence_gate(self) -> None:
        case, context = self._context("npu_matmul_fp32")
        result = deepcopy(build_reference_result(case, context))
        del result["opportunities"][0]["analysis_scope"]

        score = score_case_result(case, context, result)
        scope = next(check for check in score["checks"] if check["check_id"] == "scope_disclosure")

        self.assertFalse(scope["passed"])
        self.assertEqual(score["validated_result"]["recommendation"], "evidence_required")

    def test_ipc_gpu_hallucination_fails_hardware_rejection(self) -> None:
        case, context = self._context("ipc_proxy_sync_negative")
        source_sha = context["source"]["sha256"]
        hallucinated = {
            "schema_version": "opportunity_reasoning/v1",
            "recommendation": "evidence_required",
            "confidence": "low",
            "evidence_status": "insufficient_evidence",
            "summary": "同步 IPC 需要进一步验证。",
            "opportunities": [
                {
                    "opportunity_kind": "heterogeneous",
                    "candidate_target": "GPU shader",
                    "rationale": "错误示例。",
                    "required_conditions": [],
                    "risks": [],
                    "evidence_ids": [f"source:{source_sha}"],
                    "verification_plan": [],
                    "implementation_sketch": "",
                }
            ],
            "missing_evidence": ["调用方模式"],
            "negative_evidence": ["self time 很低"],
        }
        score = score_case_result(case, context, hallucinated)

        hardware_check = next(check for check in score["checks"] if check["check_id"] == "hardware_rejection")
        self.assertFalse(hardware_check["passed"])
        self.assertLess(score["score"], 100.0)

    def test_verification_wording_is_diagnostic_not_a_brittle_keyword_gate(self) -> None:
        case, context = self._context("npu_matmul_fp32")
        result = deepcopy(build_reference_result(case, context))
        result["missing_evidence"] = ["需要在真实目标上执行可复现实验。"]
        result["opportunities"][0]["verification_plan"] = ["建立独立的运行时验证实验。"]

        score = score_case_result(case, context, result)
        verification = next(check for check in score["checks"] if check["check_id"] == "verification_and_cost_gates")

        self.assertTrue(verification["passed"])
        self.assertFalse(verification["semantic_keyword_diagnostic_passed"])

    def test_prepare_and_score_endpoints_rebuild_the_same_context(self) -> None:
        prepared = platform_app.prepare_hardware_strategy_evaluation_case(
            "npu_matmul_fp32",
            platform_app.HardwareStrategyEvaluationPrepareRequest(),
        )
        self.assertNotIn("oracle", json.dumps(prepared, ensure_ascii=False))
        case = case_by_id(self.suite, "npu_matmul_fp32")
        result = build_reference_result(case, prepared["context"])
        scored = platform_app.score_hardware_strategy_evaluation_case(
            "npu_matmul_fp32",
            platform_app.HardwareStrategyEvaluationScoreRequest(
                context_sha256=prepared["context"]["context_sha256"],
                result=result,
            ),
        )
        self.assertEqual(scored["score"]["score"], 100.0)

    def test_prepare_endpoint_never_returns_fixture_oracle(self) -> None:
        prepared = platform_app.prepare_hardware_strategy_evaluation_case(
            "gpu_rgba_brightness",
            platform_app.HardwareStrategyEvaluationPrepareRequest(),
        )

        serialized = json.dumps(prepared, ensure_ascii=False)
        self.assertNotIn('"oracle"', serialized)
        self.assertNotIn("required_api_evidence_ids", serialized)

    def test_public_score_hides_oracle_expectations(self) -> None:
        case, context = self._context("npu_matmul_fp32")
        internal = score_case_result(case, context, build_reference_result(case, context))
        public = public_score_result(internal)
        serialized = json.dumps(public, ensure_ascii=False)

        self.assertTrue(public["passed"])
        self.assertNotIn("required_api_evidence_ids", serialized)
        self.assertNotIn("expected_evidence_ids", serialized)
        self.assertNotIn("expected_units", serialized)
        self.assertNotIn("hardware_expected", serialized)


if __name__ == "__main__":
    unittest.main()
