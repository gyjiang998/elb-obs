from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.core import ArtifactStore, PlatformError, atomic_write_json
from backend.parallel_modeling.importer import capture_dataset_name
from backend.trace_library import TraceLibrary, bridge_copy_identity, create_trace_library_router, trace_identity


class LibraryGateway:
    def __init__(self):
        self.bridges = [{"bridge_id": "bridge_test", "connection_state": "connected", "display_name": "Test Windows"}]
        self.items = []
        self.jobs = []
        self.fail = False

    def list_bridges(self, *, project_id):
        return self.bridges

    def _bridge_job(self, **kwargs):
        self.jobs.append(kwargs)
        return {**kwargs, "job_id": "devjob_" + "a" * 24}

    def dispatch_bridge_job(self, job, *, wait_seconds):
        if self.fail:
            raise PlatformError("BRIDGE_OFFLINE", "offline", status_code=503)
        if job["action"] == "trace.library.delete":
            item = next(item for item in self.items if item["trace_id"] == job["payload"]["trace_id"])
            item["location"]["status"] = "deleted"
            return {"result": {"status": "succeeded", "item": item}}
        return {"result": {"status": "succeeded", "items": self.items}}


class RecordingModeling:
    def __init__(self, *, failure: PlatformError | None = None):
        self.calls = []
        self.failure = failure

    def import_dataset(self, payload):
        self.calls.append(payload)
        if self.failure:
            raise self.failure
        step_id = payload["step_id"]
        return {"schema_version": "parallel_modeling_dataset/v1", "id": "ds_" + "e" * 24,
                "name": payload["name"], "status": "ready", "source_kind": "capture_manifest",
                "provenance": {"capture": {"step_id": step_id}}}


class TraceLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = ArtifactStore(self.root, project_id="trace-project")
        self.gateway = LibraryGateway()
        self.library = TraceLibrary(self.root, self.store, self.gateway)
        self.run_id = "capture_" + "a" * 24
        self.relative = "07_edit/trace.ftrace"
        self.trace_id = trace_identity(self.store.project_id, self.run_id, self.relative)
        self.managed = f"scenario-capture/runs/{self.run_id}/{self.relative}"
        self.raw = self.root / self.managed
        self.raw.parent.mkdir(parents=True)
        self.content = b"ftrace original\n"
        self.raw.write_bytes(self.content)
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.run_path = self.raw.parent.parent / "run.json"
        self.run = {"project_id": self.store.project_id, "run_id": self.run_id, "plan_id": "art_" + "a" * 24,
            "status": "succeeded", "created_at": "2026-10-16T00:00:00Z", "connection_mode": "local", "device_serial": "phone",
            "manifest": {"files": [{"kind": "trace", "step_id": "step_07", "name": "trace.ftrace",
                "relative_path": self.relative, "managed_path": self.managed, "size_bytes": len(self.content), "sha256": self.digest}],
                "steps": [{"step_id": "step_07", "title": "点击编辑"}]}}
        atomic_write_json(self.run_path, self.run)
        self.environment = patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "device"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        app = FastAPI()
        @app.exception_handler(PlatformError)
        async def errors(request: Request, exc: PlatformError):
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code)
        app.include_router(create_trace_library_router(self.library))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def item(self):
        return next(item for item in self.library.list()["items"] if item["trace_id"] == self.trace_id)

    def bridge_item(self):
        return {"trace_id": self.trace_id, "project_id": self.store.project_id, "run_id": self.run_id,
            "name": "trace.ftrace", "step_id": "step_07", "size_bytes": len(self.content), "sha256": self.digest,
            "location": {"copy_id": bridge_copy_identity(self.store.project_id, "bridge_test", self.trace_id),
                "bridge_id": "bridge_test", "local_path": "C:\\Bridge\\traces\\trace.ftrace", "status": "available"}}

    def write_artifact(self, kind, payload):
        return self.store.write(kind, payload, source_connector_id="test", source_binding={}, producer={})

    def test_indexes_existing_capture_without_moving_or_changing_files(self):
        before = self.raw.stat().st_ino
        item = self.item()
        self.assertEqual(item["step_title"], "点击编辑")
        self.assertEqual(item["locations"][0]["local_path"], str(self.raw))
        self.assertEqual(item["locations"][0]["label"], "本机原始文件")
        self.assertEqual(item["locations"][0]["connection_mode"], "local")
        self.assertEqual(self.raw.stat().st_ino, before)
        self.assertFalse(self.gateway.jobs)

    def test_download_and_exact_copy_deletion_keep_metadata_and_other_files(self):
        unrelated = self.raw.parent / "notes.txt"
        unrelated.write_text("keep")
        item = self.item()
        copy_id = item["locations"][0]["copy_id"]
        response = self.client.get(f"/v1/trace-library/{self.trace_id}/download", params={"copy_id": copy_id})
        self.assertEqual(response.content, self.content)
        response = self.client.delete(f"/v1/trace-library/{self.trace_id}/copies/{copy_id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["item"]["locations"][0]["status"], "deleted")
        self.assertFalse(self.raw.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(self.run_path.exists())
        self.assertEqual(self.item()["locations"][0]["status"], "deleted")
        missing = self.client.get(f"/v1/trace-library/{self.trace_id}/download", params={"copy_id": copy_id})
        self.assertEqual(missing.status_code, 404)

    def test_static_available_manifest_is_rechecked_and_external_missing_is_visible(self):
        self.item()
        self.raw.unlink()
        self.assertEqual(self.item()["locations"][0]["status"], "missing")
        self.assertFalse(self.item()["locations"][0]["can_download"])

    def test_changed_file_is_not_deleted_or_downloaded(self):
        copy_id = self.item()["locations"][0]["copy_id"]
        self.raw.write_bytes(b"X" * len(self.content))
        for operation in (self.library.delete_copy, self.library.download):
            with self.assertRaises(PlatformError) as error:
                operation(self.trace_id, copy_id)
            self.assertEqual(error.exception.code, "TRACE_FILE_CHANGED")
        self.assertTrue(self.raw.exists())

    def test_active_capture_cannot_be_deleted(self):
        copy_id = self.item()["locations"][0]["copy_id"]
        self.run["status"] = "running"
        atomic_write_json(self.run_path, self.run)
        with self.assertRaises(PlatformError) as error:
            self.library.delete_copy(self.trace_id, copy_id)
        self.assertEqual(error.exception.code, "TRACE_CAPTURE_ACTIVE")
        self.assertFalse(self.item()["locations"][0]["can_delete"])

    def test_path_traversal_and_symlink_are_rejected(self):
        for value in ("../secret", "/tmp/secret", "artifacts/trace.json"):
            with self.assertRaises(PlatformError):
                self.library._safe_path(value)
        copy_id = self.item()["locations"][0]["copy_id"]
        outside = self.root / "outside"
        outside.write_bytes(self.content)
        self.raw.unlink()
        self.raw.symlink_to(outside)
        with self.assertRaises(PlatformError):
            self.library.delete_copy(self.trace_id, copy_id)
        self.assertTrue(outside.exists())

    def test_project_inventory_is_isolated(self):
        other = TraceLibrary(self.root, ArtifactStore(self.root, project_id="other"), self.gateway)
        self.assertEqual(other.list()["items"], [])
        self.assertNotEqual(self.library.index_path, other.index_path)
        with self.assertRaises(PlatformError):
            other.delete_copy(self.trace_id, self.item()["locations"][0]["copy_id"])

    def test_raw_upload_and_modeling_trace_are_managed_as_distinct_files(self):
        raw = self.root / "raw-inputs" / "uploaded.htrace"
        raw.parent.mkdir()
        raw.write_bytes(self.content)
        self.write_artifact("raw_trace_input", {"input_kind": "raw_htrace", "managed_path": "raw-inputs/uploaded.htrace",
            "file_name": "uploaded.htrace", "bytes": len(self.content), "input_sha256": self.digest})
        dataset_id = "ds_" + "b" * 24
        model_raw = self.root / "parallel-modeling" / "inputs" / dataset_id / "trace.ftrace"
        model_raw.parent.mkdir(parents=True)
        model_raw.write_bytes(self.content)
        self.write_artifact("parallel_modeling_dataset", {"id": dataset_id, "name": "Imported Trace", "provenance": {}})
        items = self.library.list()["items"]
        self.assertEqual({item["source"] for item in items}, {"scenario_capture", "raw_trace_input", "parallel_modeling"})
        model = next(item for item in items if item["source"] == "parallel_modeling")
        self.library.delete_copy(model["trace_id"], model["locations"][0]["copy_id"])
        self.assertFalse(model_raw.exists())
        self.assertTrue(raw.exists())
        self.assertTrue(self.raw.exists())

    def test_import_local_capture_package_streams_standard_run_into_library(self):
        run_id = "capture_" + "c" * 24
        relative = "04_build/trace.ftrace"
        content = b"local windows trace\n"
        digest = hashlib.sha256(content).hexdigest()
        manifest = {
            "schema_version": "scenario_capture_manifest/v1", "run_id": run_id,
            "plan_id": "art_" + "d" * 24, "project_id": "desktop-local", "status": "succeeded",
            "created_at": "2026-10-26T00:00:00Z", "device_serial": "PHONE-WIN",
            "local_execution": {"host": "WIN-CAPTURE", "run_directory": r"D:\AgenticCapture\capture_cccccccccccccccccccccccc"},
            "steps": [{"step_id": "step_04", "title": "BuildItem", "captured": True}],
            "files": [{"kind": "trace", "step_id": "step_04", "name": "trace.ftrace",
                       "relative_path": relative, "size_bytes": len(content), "sha256": digest}],
            "quality": {"warnings": [], "errors": []},
        }
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest))
            bundle.writestr(relative, content)
        response = self.client.post("/v1/trace-library/import-capture-package", content=stream.getvalue(),
                                    headers={"Content-Type": "application/zip", "X-Capture-Filename": "local.capture.zip"})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "imported")
        self.assertEqual(payload["run"]["project_id"], self.store.project_id)
        self.assertEqual(payload["run"]["local_capture_package"]["portable_project_id"], "desktop-local")
        item = next(value for value in payload["catalog"]["items"] if value.get("run_id") == run_id)
        self.assertEqual(item["sha256"], digest)
        self.assertEqual({location["status"] for location in item["locations"]}, {"available", "external"})
        windows = next(location for location in item["locations"] if location["kind"] == "windows_local")
        self.assertEqual(windows["local_path"], r"D:\AgenticCapture\capture_cccccccccccccccccccccccc\04_build\trace.ftrace")
        self.assertFalse(windows["can_delete"])
        imported = self.root / "scenario-capture" / "runs" / run_id / relative
        self.assertEqual(imported.read_bytes(), content)

        repeated = self.client.post("/v1/trace-library/import-capture-package", content=stream.getvalue(),
                                    headers={"Content-Type": "application/zip"})
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["status"], "already_imported")

    def test_capture_package_creates_analysis_dataset_from_trace_pmu_hilog_and_manifest(self):
        modeling = RecordingModeling()
        self.library.modeling = modeling
        run_id = "capture_" + "e" * 24
        files = {
            "04_build/trace.ftrace": b"trace\n",
            "04_build/pmu.csv": b"timestamp_ns,cycles,instructions\n100,20,10\n",
            "04_build/pmu.json": b'{"target_tid":1,"clock":"monotonic"}\n',
            "04_build/hilog.log": b"BuildItem log\n",
        }
        kinds = {"trace.ftrace": "trace", "pmu.csv": "pmu", "pmu.json": "pmu_metadata", "hilog.log": "hilog"}
        manifest = {
            "schema_version": "scenario_capture_manifest/v1", "run_id": run_id,
            "plan_id": "art_" + "f" * 24, "project_id": "desktop-local", "status": "succeeded",
            "created_at": "2026-10-28T00:00:00Z", "device_serial": "PHONE-WIN", "title": "图库17步",
            "steps": [{"step_id": "step_04", "ordinal": 4, "title": "BuildItem 编辑", "captured": True,
                       "collector_status": {"trace": "collected", "pmu": "collected", "hilog": "collected"},
                       "trace_validation": {"valid": True}}],
            "files": [{"kind": kinds[Path(name).name], "step_id": "step_04", "name": Path(name).name,
                       "relative_path": name, "size_bytes": len(content),
                       "sha256": hashlib.sha256(content).hexdigest()} for name, content in files.items()],
            "quality": {"warnings": [], "errors": []},
        }
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest))
            for name, content in files.items():
                bundle.writestr(name, content)
        response = self.client.post("/v1/trace-library/import-capture-package", content=stream.getvalue(),
                                    headers={"Content-Type": "application/zip"})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["analysis_ready_count"], 1)
        self.assertEqual(payload["datasets"][0]["id"], "ds_" + "e" * 24)
        self.assertEqual(payload["dataset_imports"][0]["status"], "ready")
        self.assertEqual(payload["dataset_imports"][0]["file_kinds"], ["hilog", "pmu", "pmu_metadata", "trace"])
        self.assertEqual(len(modeling.calls), 1)
        normalized = modeling.calls[0]["capture_manifest"]
        self.assertEqual(modeling.calls[0]["step_id"], "step_04")
        self.assertEqual(modeling.calls[0]["name"], "[图库17步] Step 4: BuildItem 编辑")
        self.assertEqual({entry["kind"] for entry in normalized["files"]}, set(kinds.values()))
        for entry in normalized["files"]:
            self.assertEqual(entry["managed_path"], f"scenario-capture/runs/{run_id}/{entry['relative_path']}")
            self.assertEqual((self.root / entry["managed_path"]).read_bytes(), files[entry["relative_path"]])

    def test_capture_dataset_name_does_not_invent_missing_plan_title(self):
        self.assertEqual(capture_dataset_name({}, {"step_id": "step_07", "title": "点击编辑"}),
                         "Step 7: 点击编辑")
        self.assertEqual(capture_dataset_name({}, {"step_id": "custom", "title": "点击编辑"}),
                         "点击编辑")

    def test_non_modelable_step_keeps_whole_capture_package_and_reports_reason(self):
        modeling = RecordingModeling(failure=PlatformError(
            "parallel_modeling_invalid_input", "Trace 中没有完整 BuildItem 回调。", details={"step_id": "step_04"}))
        self.library.modeling = modeling
        run_id = "capture_" + "f" * 24
        relative = "04_build/trace.ftrace"
        content = b"valid archive but not modelable\n"
        manifest = {
            "schema_version": "scenario_capture_manifest/v1", "run_id": run_id,
            "plan_id": "art_" + "e" * 24, "project_id": "desktop-local", "status": "succeeded", "device_serial": "PHONE-WIN",
            "steps": [{"step_id": "step_04", "title": "BuildItem", "captured": True,
                       "trace_validation": {"valid": True}}],
            "files": [{"kind": "trace", "step_id": "step_04", "name": "trace.ftrace",
                       "relative_path": relative, "size_bytes": len(content),
                       "sha256": hashlib.sha256(content).hexdigest()}],
        }
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest))
            bundle.writestr(relative, content)
        response = self.client.post("/v1/trace-library/import-capture-package", content=stream.getvalue(),
                                    headers={"Content-Type": "application/zip"})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["analysis_ready_count"], 0)
        self.assertEqual(payload["dataset_imports"][0]["status"], "not_analyzable")
        self.assertEqual(payload["dataset_imports"][0]["error"]["code"], "parallel_modeling_invalid_input")
        self.assertTrue((self.root / "scenario-capture" / "runs" / run_id / relative).is_file())

    def test_unsafe_capture_zip_never_reaches_modeling_import(self):
        modeling = RecordingModeling()
        self.library.modeling = modeling
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", "{}")
            bundle.writestr("../escape", "not allowed")
        response = self.client.post("/v1/trace-library/import-capture-package", content=stream.getvalue(),
                                    headers={"Content-Type": "application/zip"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "LOCAL_CAPTURE_PATH_INVALID")
        self.assertEqual(modeling.calls, [])

    def test_capture_model_copy_and_gateway_transport_copy_have_independent_ids(self):
        job_id = "devjob_" + "c" * 24
        transport = self.root / "device-gateway" / "bridge-collected" / f"{job_id}-{self.digest[:16]}.bin"
        transport.parent.mkdir(parents=True)
        transport.write_bytes(self.content)
        self.run["manifest"]["files"][0]["device_job_id"] = job_id
        atomic_write_json(self.run_path, self.run)
        dataset_id = "ds_" + "d" * 24
        model = self.root / "parallel-modeling" / "inputs" / dataset_id / "trace.ftrace"
        model.parent.mkdir(parents=True)
        model.write_bytes(self.content)
        self.write_artifact("parallel_modeling_dataset", {"id": dataset_id, "name": "Dataset", "provenance": {
            "capture": {"run_id": self.run_id, "step_id": "step_07", "trace_sha256": self.digest}}})
        item = self.item()
        self.assertEqual(len(item["locations"]), 3)
        self.assertEqual(len({location["copy_id"] for location in item["locations"]}), 3)
        selected = next(location for location in item["locations"] if location["label"] == "平台回传缓存")
        self.library.delete_copy(self.trace_id, selected["copy_id"])
        self.assertFalse(transport.exists())
        self.assertTrue(self.raw.exists())
        self.assertTrue(model.exists())

    def test_bridge_refresh_prefers_windows_original_and_remote_delete_keeps_server(self):
        self.gateway.items = [self.bridge_item()]
        result = self.library.refresh("bridge_test")
        item = result["items"][0]
        selected = next(location for location in item["locations"] if location["kind"] == "windows_bridge")
        self.assertEqual(item["preferred_copy_id"], selected["copy_id"])
        self.assertFalse(selected["can_download"])
        self.library.delete_copy(self.trace_id, selected["copy_id"])
        self.assertEqual(self.gateway.jobs[-1]["action"], "trace.library.delete")
        self.assertEqual(self.gateway.jobs[-1]["payload"], {"trace_id": self.trace_id, "copy_id": selected["copy_id"]})
        self.assertTrue(self.raw.exists())
        self.assertEqual(self.gateway.jobs[-1]["project_id"], self.store.project_id)
        self.assertEqual(self.gateway.jobs[-1]["lease_id"], "")

    def test_offline_or_unverified_bridge_is_not_claimed_available(self):
        self.gateway.items = [self.bridge_item()]
        self.library.refresh()
        self.gateway.fail = True
        result = self.library.refresh()
        remote = next(loc for loc in result["items"][0]["locations"] if loc["kind"] == "windows_bridge")
        self.assertEqual(remote["status"], "unknown")
        self.assertFalse(remote["can_delete"])
        self.gateway.bridges[0]["connection_state"] = "offline"
        remote = next(loc for loc in self.item()["locations"] if loc["kind"] == "windows_bridge")
        self.assertEqual(remote["status"], "offline")

    def test_fresh_bridge_missing_overrides_old_capture_manifest_available(self):
        retained = self.bridge_item()
        retained["location"]["kind"] = "windows_bridge"
        self.run["bridge_id"] = "bridge_test"
        self.run["manifest"]["files"][0]["storage_locations"] = [dict(retained["location"])]
        atomic_write_json(self.run_path, self.run)
        retained["location"]["status"] = "missing"
        self.gateway.items = [retained]
        self.library.refresh()
        remote = next(loc for loc in self.item()["locations"] if loc["kind"] == "windows_bridge")
        self.assertEqual(remote["status"], "missing")
        self.assertFalse(remote["can_delete"])

    def test_modeling_import_reports_deleted_capture_file_missing(self):
        from backend.parallel_modeling.importer import capture_inputs
        copy_id = self.item()["locations"][0]["copy_id"]
        self.library.delete_copy(self.trace_id, copy_id)
        manifest = {**self.run["manifest"], "schema_version": "scenario_capture_manifest/v1",
            "steps": [{"step_id": "step_07", "captured": True, "trace_validation": {"valid": True}}]}
        with self.assertRaises(PlatformError) as error:
            capture_inputs({"capture_manifest": manifest, "step_id": "step_07"}, self.root)
        self.assertIn("missing", str(error.exception))

    def test_bridge_project_and_deployment_mode_are_checked(self):
        with self.assertRaises(PlatformError) as error:
            self.library.refresh("foreign-bridge")
        self.assertEqual(error.exception.code, "TRACE_BRIDGE_UNAUTHORIZED")
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "analysis"}):
            result = self.library.refresh()
        self.assertEqual(result["diagnostics"][0]["code"], "DEPLOYMENT_DEVICE_VALIDATION_DISABLED")
        self.assertFalse(self.gateway.jobs)


if __name__ == "__main__":
    unittest.main()
