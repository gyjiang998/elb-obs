from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.parallel_modeling.manager import ParallelModelingManager
from backend.tests.test_parallel_modeling import trace_fixture


def hardware_profile(serial: str, name: str, *, conflicting: bool = False) -> dict:
    groups = [
        {"id": "small", "kind": "small", "physical_core_count": 2,
         "logical_cpu_ids": [0, 1], "opps_khz": [500_000, 1_000_000]},
        {"id": "middle", "kind": "middle", "physical_core_count": 2,
         "logical_cpu_ids": [2, 3], "opps_khz": [800_000, 1_800_000]},
        {"id": "big", "kind": "big", "physical_core_count": 1,
         "logical_cpu_ids": [4, 5], "opps_khz": [1_000_000, 2_400_000]},
    ]
    if conflicting:
        groups[0].update(physical_core_count=1, logical_cpu_ids=[0])
        groups[1].update(physical_core_count=3, logical_cpu_ids=[1, 2, 3])
    return {
        "schema_version": "parallel_hardware_profile/v1", "name": name,
        "source": "local_hdc", "cpu_groups": groups,
        "evidence": {"device_serial_sha256": hashlib.sha256(serial.encode()).hexdigest()},
    }


class CaptureHardwareBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = ParallelModelingManager(self.root)
        self.relative = Path("scenario-capture/runs/capture_identity/step-07/trace.ftrace")
        self.trace = self.root / self.relative
        self.trace.parent.mkdir(parents=True)
        self.trace.write_text(trace_fixture())

    def manifest(self, serial: str = "PHONE-IDENTITY") -> dict:
        return {
            "schema_version": "scenario_capture_manifest/v1", "run_id": "capture_identity",
            "plan_id": "plan_identity", "device_serial": serial,
            "steps": [{"step_id": "step-07", "captured": True, "target_pid": 10,
                       "collector_status": {"trace": "collected"},
                       "trace_validation": {"valid": True}}],
            "files": [{"step_id": "step-07", "kind": "trace",
                       "managed_path": str(self.relative),
                       "sha256": hashlib.sha256(self.trace.read_bytes()).hexdigest()}],
        }

    def import_capture(self, manifest: dict, **extra):
        return self.manager.import_dataset({"capture_manifest": manifest, "step_id": "step-07", **extra})

    def test_unbound_capture_reimport_after_profile_registration_creates_bound_revision(self):
        manifest = self.manifest()
        unbound = self.import_capture(manifest)
        self.assertIsNone(unbound["hardware_profile_id"])
        self.assertEqual(unbound["quality"]["hardware_binding_status"], "no_matching_active_profile")

        profile = self.manager.create_hardware_profile(hardware_profile("PHONE-IDENTITY", "提取画像"))
        bound = self.import_capture(manifest)
        self.assertNotEqual(bound["id"], unbound["id"])
        self.assertEqual(bound["hardware_profile_id"], profile["id"])
        self.assertTrue(bound["quality"]["hardware_identity_verified"])
        self.assertEqual(bound["provenance"]["hardware_resolution"]["source"], "device_serial_sha256")
        self.assertIsNone(self.manager.get_dataset(unbound["id"])["hardware_profile_id"])
        self.assertNotIn("PHONE-IDENTITY", str(bound))

        features = self.manager.workload_features.analyze({"dataset_id": bound["id"]})
        self.assertEqual(features["stages"][0]["scheduling"]["cpu_placement"]["primary_core_group_id"], "small")

    def test_multiple_compatible_profiles_select_latest_with_auditable_candidates(self):
        with patch("backend.parallel_modeling.hardware_profiles.utc_now", return_value="2026-10-29T00:00:00Z"):
            older = self.manager.create_hardware_profile(hardware_profile("PHONE-IDENTITY", "旧画像"))
        newer_payload = hardware_profile("PHONE-IDENTITY", "新画像")
        newer_payload["cpu_groups"][0]["opps_khz"] = [400_000, 1_100_000]
        with patch("backend.parallel_modeling.hardware_profiles.utc_now", return_value="2026-10-30T00:00:00Z"):
            newer = self.manager.create_hardware_profile(newer_payload)

        dataset = self.import_capture(self.manifest())
        resolution = dataset["provenance"]["hardware_resolution"]
        self.assertEqual(dataset["hardware_profile_id"], newer["id"])
        self.assertEqual(resolution["status"], "matched_latest_compatible_profile")
        self.assertEqual(resolution["candidate_count"], 2)
        self.assertEqual(set(resolution["candidate_hardware_profile_ids"]), {older["id"], newer["id"]})
        self.assertEqual(resolution["selection_policy"], "latest_created_at_then_profile_id")

    def test_conflicting_topologies_stay_unbound_without_explicit_selection(self):
        first = self.manager.create_hardware_profile(hardware_profile("PHONE-IDENTITY", "拓扑 A"))
        second = self.manager.create_hardware_profile(hardware_profile("PHONE-IDENTITY", "拓扑 B", conflicting=True))
        dataset = self.import_capture(self.manifest())
        resolution = dataset["provenance"]["hardware_resolution"]
        self.assertIsNone(dataset["hardware_profile_id"])
        self.assertFalse(dataset["quality"]["can_search"])
        self.assertEqual(resolution["status"], "ambiguous_conflicting_cpu_topology")
        self.assertEqual(set(resolution["candidate_hardware_profile_ids"]), {first["id"], second["id"]})

        derived = self.manager.bind_dataset_hardware(dataset["id"], {"hardware_profile_id": first["id"]})
        self.assertNotEqual(derived["id"], dataset["id"])
        self.assertEqual(derived["hardware_profile_id"], first["id"])
        self.assertIsNone(self.manager.get_dataset(dataset["id"])["hardware_profile_id"])

    def test_import_request_binding_takes_priority_and_records_verification_risk(self):
        selected = self.manager.create_hardware_profile(hardware_profile("OTHER-PHONE", "用户选择画像"))
        self.manager.create_hardware_profile(hardware_profile("PHONE-IDENTITY", "自动匹配画像"))
        dataset = self.import_capture(self.manifest(), hardware_profile_id=selected["id"])
        resolution = dataset["provenance"]["hardware_resolution"]
        self.assertEqual(dataset["hardware_profile_id"], selected["id"])
        self.assertEqual(resolution["status"], "matched_import_request_profile")
        self.assertEqual(resolution["source"], "import_request_binding")
        self.assertFalse(dataset["quality"]["hardware_identity_verified"])
        self.assertEqual(dataset["quality"]["prediction_applicability"], "import_request_bound_profile_prediction")

    def test_manifest_recorded_profile_is_honored_but_mismatch_is_not_verified(self):
        profile = self.manager.create_hardware_profile(hardware_profile("OTHER-PHONE", "采集记录画像"))
        manifest = self.manifest()
        manifest["hardware_profile_id"] = profile["id"]
        dataset = self.import_capture(manifest)
        self.assertEqual(dataset["hardware_profile_id"], profile["id"])
        self.assertFalse(dataset["quality"]["hardware_identity_verified"])
        self.assertEqual(dataset["provenance"]["hardware_resolution"]["source"], "capture_record_binding")


if __name__ == "__main__":
    unittest.main()
