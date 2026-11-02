"""Deployable archives preserve independent samples and their measurement limits."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core import ArtifactStore, PlatformError, sha256_file
from backend.parallel_modeling.manager import (
    BUNDLE_ROOT, BUILTIN_ID, FULL_PMU_BUILTIN_ID, HARDWARE_ID, ParallelModelingManager,
)
from backend.trace_library import TraceLibrary, create_trace_library_router


class NoDeviceGateway:
    def list_bridges(self, *, project_id):
        return []

    def _bridge_job(self, **kwargs):
        raise AssertionError("An archived demo must not create device jobs")

    def dispatch_bridge_job(self, *args, **kwargs):
        raise AssertionError("An archived demo must not dispatch device jobs")


class ParallelModelingBuiltinTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = ParallelModelingManager(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_catalog_preserves_original_and_exposes_separate_full_group_archive(self):
        rows = self.manager.catalog()["builtins"]
        self.assertEqual([row["id"] for row in rows], [BUILTIN_ID, FULL_PMU_BUILTIN_ID])
        self.assertEqual(rows[0]["hardware_profile_id"], HARDWARE_ID)
        self.assertEqual(rows[0]["metrics"]["duration_ms"], 86.94)
        self.assertIsNone(rows[1]["hardware_profile_id"])
        self.assertEqual(rows[1]["prediction_applicability"], "analysis_only")
        self.assertIn("复用估计", rows[1]["name"])
        rows[1]["metrics"]["duration_ms"] = 0
        self.assertEqual(self.manager.catalog()["builtins"][1]["metrics"]["duration_ms"], 136.517)

    def test_full_archive_resolves_verified_hardware_without_rewriting_source(self):
        dataset = self.manager.import_dataset({"builtin_id": FULL_PMU_BUILTIN_ID,
                                               "hardware_profile_id": HARDWARE_ID})
        self.assertIsNone(dataset["hardware_profile_id"])
        self.assertFalse(dataset["quality"]["hardware_identity_verified"])
        self.assertFalse(dataset["quality"]["can_search"])
        self.assertEqual(dataset["quality"]["prediction_applicability"], "analysis_only")
        self.assertEqual(dataset["summary"]["duration_ms"], 136.517)
        self.assertEqual(dataset["summary"]["builditem_count"], 26)
        self.assertEqual(dataset["summary"]["service_call_count"], 73)
        self.assertTrue(dataset["quality"]["pmu"]["clock_verified_with_marker"])
        analysis = self.manager.analyze({"dataset_id": dataset["id"]})
        self.assertTrue(analysis["replay_gate"]["passed"])
        result = self.manager.search({"analysis_id": analysis["id"], "space": {
            "available_core_group_ids": ["big", "middle"], "worker_counts": [0, 1, 2, 3],
            "frequency_policy": "max_opps"}})
        self.assertEqual(result["hardware_resolution"]["source"], "verified_archived_sample")
        self.assertEqual(result["hardware_resolution"]["hardware_profile_id"], HARDWARE_ID)
        self.assertTrue(result["hardware_resolution"]["hardware_identity_verified"])
        self.assertEqual(self.manager.get_dataset(dataset["id"]), dataset)
        self.assertEqual(result["model_package"]["validation"]["status"], "not_application_validated")
        self.assertEqual(len(result["core_combination_recommendations"]), 4)

    def test_archive_retains_estimates_missing_windows_and_zero_counter_evidence(self):
        dataset = self.manager.import_dataset({"builtin_id": FULL_PMU_BUILTIN_ID})
        directory = self.manager.state_root / "inputs" / dataset["id"]
        bundled = json.loads((BUNDLE_ROOT / (FULL_PMU_BUILTIN_ID + ".json")).read_text())
        saved = json.loads((directory / "parsed.json").read_text())
        self.assertEqual(saved, bundled)
        self.assertEqual(set(saved["pmu_quality"]["groups"]), {"base", "cache", "branch", "stall", "l1d", "llc", "dtlb"})
        self.assertIn("branch_instructions", saved["pmu_quality"]["zero_increment_events"])
        archive = dataset["provenance"]["sample_archive"]
        for name, evidence in archive["raw_archive"]["files"].items():
            self.assertEqual(sha256_file(directory / name), evidence["sha256"], name)
            self.assertEqual((directory / name).stat().st_size, evidence["size_bytes"], name)
        self.assertTrue((directory / "manifest.json").is_file())
        for name in ("trace.ftrace", "pmu.csv", "pmu.json"):
            self.assertEqual(sha256_file(directory / name), archive["raw_source_files"][name]["sha256"])
        profile = self.manager.workload_features.analyze({"dataset_id": dataset["id"]})
        self.assertEqual(profile["provenance"]["pmu_estimate_rebuild"]["status"], "stored_evidence")
        self.assertEqual(len(profile["stages"]), 26)
        self.assertTrue(all(stage["metric_evidence"]["instructions"]["estimate"] is not None for stage in profile["stages"]))
        branch = [stage["metric_evidence"]["branch_instructions"] for stage in profile["stages"]]
        self.assertTrue(any(item["estimate"] is None for item in branch))
        self.assertTrue(any(item["estimate"] == 0 for item in branch))
        self.assertFalse(profile["quality"]["hardware_identity_verified"])
        self.assertTrue(archive["raw_files_included"])
        self.assertFalse(archive["pmu_quality_complete"])
        self.assertEqual(archive["zero_increment_entire_capture"], ["branch_instructions"])
        self.assertEqual(archive["raw_counter_deltas_entire_capture"]["branch"]["branch_instructions"], 0)
        self.assertGreater(archive["raw_counter_deltas_entire_capture"]["branch"]["branch_misses"], 0)
        self.assertEqual(archive["raw_source_files"]["trace.ftrace"]["sha256"], saved["selection"]["trace_sha256"])
        self.assertEqual(hashlib.sha256((BUNDLE_ROOT / (FULL_PMU_BUILTIN_ID + ".json")).read_bytes()).hexdigest(),
                         self.manager.bundle_provenance["files"][FULL_PMU_BUILTIN_ID + ".json"])

    def test_old_sample_remains_searchable_and_distinct(self):
        old = self.manager.import_dataset({"builtin_id": BUILTIN_ID})
        new = self.manager.import_dataset({"builtin_id": FULL_PMU_BUILTIN_ID})
        self.assertNotEqual(old["id"], new["id"])
        self.assertTrue(old["quality"]["hardware_identity_verified"])
        self.assertTrue(old["quality"]["can_search"])
        self.assertEqual(old["quality"]["prediction_applicability"], "archived_reference_sample")
        self.assertEqual(old["name"], "图库 · 编辑页 · 同轮 PMU 归档")
        self.assertIn("90.409", old["provenance"]["distinct_historical_sample"])
        self.assertNotIn("distinct_historical_sample", new["provenance"])
        self.assertEqual(len(self.manager.list_datasets()), 2)
        with self.assertRaises(PlatformError):
            self.manager.import_dataset({"builtin_id": "../gallery-edit-all-pmu-20261023"})

    def test_archive_import_preserves_same_sample_live_capture_and_raw_association(self):
        parsed = json.loads((BUNDLE_ROOT / (FULL_PMU_BUILTIN_ID + ".json")).read_text())
        run_id = "capture_" + "a" * 24
        relative = Path("scenario-capture/runs") / run_id / "07_edit/trace.ftrace"
        source = self.manager.capture_state_root / relative
        source.parent.mkdir(parents=True)
        source.write_text("capture identity fixture\n")
        manifest = {"schema_version": "scenario_capture_manifest/v1", "run_id": run_id,
            "plan_id": "fixture_plan", "steps": [{"step_id": "step_07", "captured": True,
                "target_pid": 21701, "trace_validation": {"valid": True}}],
            "files": [{"step_id": "step_07", "kind": "trace", "name": "trace.ftrace",
                "managed_path": str(relative), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}

        def same_parsed_sample(payload, directory):
            # Isolate import identity from the parser; raw-file management and
            # the real capture-manifest hash/path validation remain exercised.
            (directory / "trace.ftrace").write_text(payload["trace_text"])
            return parsed

        with patch("backend.parallel_modeling.manager.raw_to_parsed", side_effect=same_parsed_sample):
            live = self.manager.import_dataset({"capture_manifest": manifest, "step_id": "step_07",
                "name": "Live capture with the same parsed PMU sample"})
        archive = self.manager.import_dataset({"builtin_id": FULL_PMU_BUILTIN_ID})
        self.assertNotEqual(live["id"], archive["id"])
        self.assertEqual(self.manager.get_dataset(live["id"]), live)
        self.assertEqual(live["provenance"]["capture"]["run_id"], run_id)
        self.assertEqual(live["source_kind"], "capture_manifest")
        self.assertEqual(archive["source_kind"], "builtin_id")
        live_directory = self.manager.state_root / "inputs" / live["id"]
        self.assertEqual((live_directory / "trace.ftrace").read_bytes(), source.read_bytes())
        archive_trace = self.manager.state_root / "inputs" / archive["id"] / "trace.ftrace"
        self.assertEqual(sha256_file(archive_trace), parsed["selection"]["trace_sha256"])
        self.assertNotEqual(sha256_file(archive_trace), sha256_file(source))
        self.assertEqual(len(self.manager.list_datasets()), 2)

    def test_fresh_server_demo_download_delete_and_reimport_without_device(self):
        root = Path(self.temporary.name)
        store = ArtifactStore(root, project_id="fresh-server-demo")
        manager = ParallelModelingManager(root, artifact_store=store)
        library = TraceLibrary(root, store, NoDeviceGateway())
        self.assertEqual(library.list()["items"], [])
        sample = manager.bundle_provenance["datasets"][FULL_PMU_BUILTIN_ID]
        repo_zip = BUNDLE_ROOT / sample["raw_archive"]["file"]
        original_zip_hash = sha256_file(repo_zip)
        dataset = manager.import_dataset({"builtin_id": FULL_PMU_BUILTIN_ID})
        profile = manager.workload_features.analyze({"dataset_id": dataset["id"]})
        self.assertEqual(len(profile["stages"]), 26)
        self.assertFalse(profile["quality"]["hardware_identity_verified"])
        self.assertEqual(library.list()["summary"]["trace_count"], 1)
        item = library.list()["items"][0]
        self.assertEqual(item["dataset_ids"], [dataset["id"]])
        self.assertEqual(item["sha256"], sample["raw_source_files"]["trace.ftrace"]["sha256"])
        location = item["locations"][0]
        self.assertEqual(location["kind"], "server")
        self.assertTrue(location["can_download"])
        self.assertTrue(location["can_delete"])
        app = FastAPI()
        app.include_router(create_trace_library_router(library))
        with TestClient(app) as client:
            response = client.get(f"/v1/trace-library/{item['trace_id']}/download",
                                  params={"copy_id": location["copy_id"]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(hashlib.sha256(response.content).hexdigest(), item["sha256"])
            response = client.delete(f"/v1/trace-library/{item['trace_id']}/copies/{location['copy_id']}")
            self.assertEqual(response.status_code, 200)
        directory = manager.state_root / "inputs" / dataset["id"]
        self.assertFalse((directory / "trace.ftrace").exists())
        self.assertTrue((directory / "pmu.csv").is_file())
        self.assertTrue((directory / "parsed.json").is_file())
        self.assertEqual(sha256_file(repo_zip), original_zip_hash)
        restored = manager.import_dataset({"builtin_id": FULL_PMU_BUILTIN_ID})
        self.assertEqual(restored["id"], dataset["id"])
        self.assertEqual(len(manager.list_datasets()), 1)
        self.assertEqual(sha256_file(directory / "trace.ftrace"), item["sha256"])
        restored_items = library.list()["items"]
        self.assertEqual(len(restored_items), 1)
        restored_location = restored_items[0]["locations"][0]
        self.assertEqual(restored_location["status"], "available")
        path, _ = library.download(item["trace_id"], restored_location["copy_id"])
        self.assertEqual(sha256_file(path), item["sha256"])
        self.assertEqual(sha256_file(repo_zip), original_zip_hash)

    def test_corrupt_archive_rejects_import_without_publishing_dataset(self):
        sample = self.manager.bundle_provenance["datasets"][FULL_PMU_BUILTIN_ID]
        sample["raw_archive"]["sha256"] = "0" * 64
        with self.assertRaises(PlatformError) as caught:
            self.manager.import_dataset({"builtin_id": FULL_PMU_BUILTIN_ID})
        self.assertEqual(caught.exception.code, "parallel_modeling_bundle_corrupt")
        self.assertEqual(self.manager.list_datasets(), [])
        self.assertEqual(list((self.manager.state_root / "inputs").iterdir()), [])

    def test_raw_member_integrity_and_paths_are_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle_root = Path(temporary)
            raw = b"fixture trace evidence\n"
            archive_path = bundle_root / "fixture.zip"
            with zipfile.ZipFile(archive_path, "w") as bundle:
                bundle.writestr("trace.ftrace", raw)
                bundle.writestr("manifest.json", "{}")
            base = {"file": archive_path.name, "sha256": sha256_file(archive_path),
                    "files": {"trace.ftrace": {"size_bytes": len(raw),
                                                "sha256": hashlib.sha256(raw).hexdigest()}}}
            cases = {
                "member_hash": {**base, "files": {"trace.ftrace": {"size_bytes": len(raw), "sha256": "0" * 64}}},
                "member_size": {**base, "files": {"trace.ftrace": {"size_bytes": len(raw) + 1,
                                                                   "sha256": hashlib.sha256(raw).hexdigest()}}},
                "unsafe_member": {**base, "files": {"../escaped.txt": base["files"]["trace.ftrace"]}},
                "archive_path": {**base, "file": "../fixture.zip"},
            }
            with patch("backend.parallel_modeling.manager.BUNDLE_ROOT", bundle_root):
                for case, archive in cases.items():
                    with self.subTest(case=case):
                        destination = bundle_root / case
                        destination.mkdir()
                        with self.assertRaises(PlatformError) as caught:
                            self.manager._unpack_builtin_raw({"raw_archive": archive}, destination)
                        self.assertEqual(caught.exception.code, "parallel_modeling_bundle_corrupt")
            self.assertFalse((bundle_root / "escaped.txt").exists())


if __name__ == "__main__":
    unittest.main()
