"""Existing unbound archives gain verified search capability without migration."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.core import PlatformError
from backend.parallel_modeling.manager import BUNDLE_ROOT, FULL_PMU_BUILTIN_ID, HARDWARE_ID, ParallelModelingManager
from backend.tests.test_parallel_modeling import trace_fixture


class ArchivedHardwareResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = ParallelModelingManager(Path(self.temp.name))
        self.parsed = json.loads((BUNDLE_ROOT / (FULL_PMU_BUILTIN_ID + ".json")).read_text())
        self.space = {"available_core_group_ids": ["big", "middle"], "worker_counts": [0, 1, 2, 3],
                      "frequency_policy": "max_opps"}

    def test_existing_raw_import_analysis_and_feature_remain_unchanged(self):
        # Same parsed input and unbound hardware as the pre-fix live capture.
        dataset = self.manager.import_dataset({"parsed": self.parsed})
        profile = self.manager.workload_features.analyze({"dataset_id": dataset["id"]})
        analysis = self.manager.analyze({"dataset_id": dataset["id"], "feature_profile_id": profile["id"]})
        analysis.pop("hardware_resolution")
        analysis_path = self.manager.state_root / "analyses" / (analysis["id"] + ".json")
        self.manager._write_json(analysis_path, analysis)
        paths = [self.manager.state_root / "datasets" / (dataset["id"] + ".json"), analysis_path,
                 self.manager.workload_features.state_root / (profile["id"] + ".json"),
                 self.manager.state_root / "inputs" / dataset["id"] / "parsed.json"]
        before = {path: path.read_bytes() for path in paths}
        result = self.manager.search({"analysis_id": analysis["id"], "space": self.space})
        self.assertEqual(result["dataset_id"], dataset["id"])
        self.assertEqual(result["feature_profile_id"], profile["id"])
        self.assertEqual(result["workload_profile"], profile)
        self.assertIsNone(dataset["hardware_profile_id"])
        self.assertFalse(profile["quality"]["hardware_identity_verified"])
        self.assertEqual(result["hardware_resolution"]["source"], "verified_archived_sample")
        self.assertTrue(result["hardware_resolution"]["hardware_identity_verified"])
        self.assertEqual(result["model_package"]["dataset_applicability"], "verified_archive_proxy_prediction")
        self.assertEqual(result["model_package"]["validation"]["status"], "not_application_validated")
        self.assertGreater(result["best"]["metrics"]["finish_ms"], 0)
        self.assertEqual(len(result["core_combination_recommendations"]), 4)
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw, str(path))
        frequency = self.manager.search({"analysis_id": analysis["id"], "space": self.space,
            "objective": "minimum_frequency_with_budget", "budget_ms": analysis["baseline"]["duration_ms"] * 2})
        self.assertIsNotNone(frequency["best"])
        self.assertEqual(len(frequency["core_combination_recommendations"]), 4)
        self.assertNotEqual(result["id"], frequency["id"])

    def test_matching_name_trace_hash_or_shape_alone_cannot_authorize_other_samples(self):
        for change in ("metadata", "counter", "trace"):
            parsed = copy.deepcopy(self.parsed)
            if change == "metadata": parsed["frame"]["notes"].append("different sample")
            elif change == "counter": parsed["component_features"][0]["pmu"]["interior_bins"] = 999
            else: parsed["selection"]["trace_sha256"] = "0" * 64
            with self.subTest(change=change):
                dataset = self.manager.import_dataset({"parsed": parsed, "name": FULL_PMU_BUILTIN_ID})
                analysis = self.manager.analyze({"dataset_id": dataset["id"]})
                self.assertEqual(analysis["hardware_resolution"]["source"], "unresolved")
                with self.assertRaises(PlatformError) as caught:
                    self.manager.search({"analysis_id": analysis["id"], "space": self.space})
                self.assertEqual(caught.exception.code, "parallel_modeling_unknown_hardware")

    def test_unknown_hardware_stays_unbound_and_error_is_actionable(self):
        dataset = self.manager.import_dataset({"trace_text": trace_fixture()})
        analysis = self.manager.analyze({"dataset_id": dataset["id"]})
        with self.assertRaises(PlatformError) as caught:
            self.manager.search({"analysis_id": analysis["id"], "space": self.space})
        self.assertEqual(caught.exception.code, "parallel_modeling_unknown_hardware")
        self.assertIn("参考硬件", caught.exception.message)

    def test_resolution_is_bound_to_model_hardware_and_bundled_evidence(self):
        dataset = self.manager.import_dataset({"parsed": self.parsed})
        analysis = self.manager.analyze({"dataset_id": dataset["id"]})
        resolution = analysis["hardware_resolution"]
        path = BUNDLE_ROOT / resolution["evidence_file"]
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), resolution["evidence_sha256"])
        evidence = json.loads(path.read_text())
        self.assertTrue(evidence["verification"]["same_capture_device"])
        self.assertEqual(evidence["verification"]["callback_cpu_residency"]["frequency_khz"], 2500000)
        model = copy.deepcopy(self.manager.model)
        model["hardware_profile_id"] = "other-hardware"
        with patch.object(self.manager.calibration, "resolve_model", return_value=model):
            with self.assertRaises(PlatformError) as caught:
                self.manager.search({"analysis_id": analysis["id"], "space": self.space})
        self.assertEqual(caught.exception.code, "parallel_modeling_unknown_hardware")
        bound = self.manager.import_dataset({"trace_text": trace_fixture(), "hardware_profile_id": HARDWARE_ID})
        self.assertEqual(self.manager.resolve_hardware(bound)["source"], "dataset_profile")


if __name__ == "__main__":
    unittest.main()
