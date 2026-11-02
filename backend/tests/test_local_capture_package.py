from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from backend.core import ArtifactStore, PlatformError
from backend import core
from backend.local_capture_package import (
    LocalCapturePackageImporter,
    LocalCapturePackageLimits,
    validate_local_capture_zip,
)
from backend.parallel_modeling.importer import capture_inputs
from backend.trace_library import TraceLibrary


class NoDeviceGateway:
    def list_bridges(self, *, project_id):
        return []


class LocalCapturePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "capture.zip"
        self.run_id = "capture_" + "a" * 24
        self.files = {
            "07_edit/trace.ftrace": b"app-1 [000] 1.0: tracing_mark_write: B|1|CustomNode:BuildItem\n",
            "07_edit/pmu.csv": b"timestamp_ns,cycles,instructions\n100,20,10\n",
            "07_edit/pmu.json": b'{"target_tid":1,"clock":"monotonic"}\n',
        }
        self.manifest = {
            "schema_version": "scenario_capture_manifest/v1",
            "run_id": self.run_id,
            "plan_id": "art_" + "b" * 24,
            "project_id": "desktop-local",
            "status": "succeeded",
            "created_at": "2026-10-26T00:00:00Z",
            "device_serial": "PHONE-01",
            "steps": [{"step_id": "step_07", "title": "编辑", "captured": True,
                       "collector_status": {"trace": "collected", "pmu": "collected"},
                       "trace_validation": {"valid": True}}],
            "files": [
                {"step_id": "step_07", "kind": {"trace.ftrace": "trace", "pmu.csv": "pmu", "pmu.json": "pmu_metadata"}[Path(name).name],
                 "name": Path(name).name, "relative_path": name, "size_bytes": len(content),
                 "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in self.files.items()
            ],
            "quality": {"warnings": [], "errors": []},
        }
        self.write_bundle()

    def write_bundle(self, *, manifest=None, files=None, extras=None, infos=None) -> None:
        manifest = self.manifest if manifest is None else manifest
        files = self.files if files is None else files
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            for name, content in files.items():
                bundle.writestr(name, content)
            for name, content in (extras or {}).items():
                bundle.writestr(name, content)
            for info, content in infos or []:
                bundle.writestr(info, content)

    def assert_code(self, code: str, callback) -> PlatformError:
        with self.assertRaises(PlatformError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_valid_package_materializes_standard_run_and_files(self):
        importer = LocalCapturePackageImporter(self.root / "state")
        result = importer.import_zip(self.archive, project_id="local-default", bridge_id="bridge_windows", storage_host="WIN-PC")
        self.assertEqual(result["status"], "imported")
        directory = self.root / "state" / "scenario-capture" / "runs" / self.run_id
        run = json.loads((directory / "run.json").read_text())
        stored_manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(run["manifest"], stored_manifest)
        self.assertEqual(run["connection_mode"], "bridge")
        self.assertEqual(run["execution_provenance"]["executor"], "windows_local_powershell")
        self.assertEqual(run["project_id"], "local-default")
        self.assertEqual(run["local_capture_package"]["portable_project_id"], "desktop-local")
        self.assertEqual(run["bridge_id"], "bridge_windows")
        self.assertEqual(run["local_capture_package"]["storage_host"], "WIN-PC")
        for entry in stored_manifest["files"]:
            path = self.root / "state" / entry["managed_path"]
            self.assertEqual(path.read_bytes(), self.files[entry["relative_path"]])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
        leftovers = [path.name for path in directory.parent.iterdir() if path.name.startswith(".") and path.name != ".local-capture-package.lock"]
        self.assertEqual(leftovers, [])

    def test_materialized_run_is_visible_to_trace_library_and_modeling_importer(self):
        state = self.root / "state"
        result = LocalCapturePackageImporter(state).import_zip(
            self.archive, project_id="local-default", bridge_id="bridge_windows")
        store = ArtifactStore(state, project_id="local-default")
        catalog = TraceLibrary(state, store, NoDeviceGateway()).list()
        self.assertEqual(catalog["summary"]["trace_count"], 1)
        self.assertEqual(catalog["items"][0]["run_id"], self.run_id)
        self.assertEqual(catalog["items"][0]["source"], "scenario_capture")
        inputs = capture_inputs({"capture_manifest": result["run"]["manifest"], "step_id": "step_07"}, state)
        self.assertEqual(inputs["trace_text"].encode(), self.files["07_edit/trace.ftrace"])
        self.assertEqual(inputs["pmu_text"].encode(), self.files["07_edit/pmu.csv"])
        self.assertEqual(inputs["pmu_metadata"]["target_tid"], 1)

    def test_same_content_is_idempotent_even_if_zip_encoding_changes(self):
        importer = LocalCapturePackageImporter(self.root / "state")
        first = importer.import_zip(self.archive, project_id="local-default", bridge_id="bridge_windows")
        self.write_bundle()
        second = importer.import_zip(self.archive, project_id="local-default", bridge_id="bridge_windows")
        self.assertEqual(first["package"]["content_sha256"], second["package"]["content_sha256"])
        self.assertEqual(second["status"], "already_imported")

    def test_windows_import_does_not_open_directories_as_files(self):
        native_open = os.open

        def windows_open(path, flags, *args, **kwargs):
            if Path(path).is_dir():
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return native_open(path, flags, *args, **kwargs)

        importer = LocalCapturePackageImporter(self.root / "state")
        with patch.object(core, "sys", SimpleNamespace(platform="win32")), \
             patch.object(core.os, "open", side_effect=windows_open):
            first = importer.import_zip(self.archive, project_id="local-default")
            repeated = importer.import_zip(self.archive, project_id="local-default")
        self.assertEqual(first["status"], "imported")
        self.assertEqual(repeated["status"], "already_imported")
        directory = self.root / "state/scenario-capture/runs" / self.run_id
        for relative, content in self.files.items():
            self.assertEqual((directory / relative).read_bytes(), content)
        self.assertEqual(json.loads((directory / "run.json").read_text())["manifest"],
                         json.loads((directory / "manifest.json").read_text()))

    def test_same_run_id_with_different_content_is_rejected(self):
        importer = LocalCapturePackageImporter(self.root / "state")
        importer.import_zip(self.archive, project_id="local-default")
        changed = dict(self.files)
        changed["07_edit/trace.ftrace"] = b"different\n"
        manifest = json.loads(json.dumps(self.manifest))
        entry = next(item for item in manifest["files"] if item["kind"] == "trace")
        entry.update(size_bytes=len(changed["07_edit/trace.ftrace"]), sha256=hashlib.sha256(changed["07_edit/trace.ftrace"]).hexdigest())
        self.write_bundle(manifest=manifest, files=changed)
        self.assert_code("LOCAL_CAPTURE_RUN_CONFLICT", lambda: importer.import_zip(self.archive, project_id="local-default"))

    def test_same_package_cannot_cross_project_or_bridge_identity(self):
        importer = LocalCapturePackageImporter(self.root / "state")
        importer.import_zip(self.archive, project_id="project-a", bridge_id="bridge-a")
        self.assert_code("LOCAL_CAPTURE_RUN_CONFLICT", lambda: importer.import_zip(
            self.archive, project_id="project-b", bridge_id="bridge-a"))
        self.assert_code("LOCAL_CAPTURE_RUN_CONFLICT", lambda: importer.import_zip(
            self.archive, project_id="project-a", bridge_id="bridge-b"))

    def test_archive_path_traversal_absolute_and_backslash_are_rejected(self):
        for unsafe in ("../escape", "/absolute", "C:/drive", "dir\\file"):
            self.write_bundle(extras={unsafe: b"x"})
            self.assert_code("LOCAL_CAPTURE_PATH_INVALID", lambda: validate_local_capture_zip(self.archive))

    def test_symlink_and_special_zip_members_are_rejected(self):
        link = zipfile.ZipInfo("unsafe-link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.write_bundle(infos=[(link, "../outside")])
        self.assert_code("LOCAL_CAPTURE_MEMBER_UNSAFE", lambda: validate_local_capture_zip(self.archive))

    def test_hash_size_missing_and_undeclared_files_are_rejected(self):
        bad_hash = json.loads(json.dumps(self.manifest))
        bad_hash["files"][0]["sha256"] = "0" * 64
        self.write_bundle(manifest=bad_hash)
        self.assert_code("LOCAL_CAPTURE_FILE_HASH_MISMATCH", lambda: LocalCapturePackageImporter(self.root / "hash-state").import_zip(self.archive, project_id="local-default"))

        bad_size = json.loads(json.dumps(self.manifest))
        bad_size["files"][0]["size_bytes"] += 1
        self.write_bundle(manifest=bad_size)
        self.assert_code("LOCAL_CAPTURE_FILE_SIZE_MISMATCH", lambda: validate_local_capture_zip(self.archive))

        self.write_bundle(files={key: value for key, value in self.files.items() if key != "07_edit/trace.ftrace"})
        self.assert_code("LOCAL_CAPTURE_FILE_MISSING", lambda: validate_local_capture_zip(self.archive))

        self.write_bundle(extras={"07_edit/notes.txt": b"not declared"})
        self.assert_code("LOCAL_CAPTURE_FILE_UNDECLARED", lambda: validate_local_capture_zip(self.archive))

    def test_schema_status_managed_path_and_identity_mismatch_are_rejected(self):
        for field, value, code in (
            ("schema_version", "other/v1", "LOCAL_CAPTURE_SCHEMA_UNSUPPORTED"),
            ("status", "running", "LOCAL_CAPTURE_STATUS_INVALID"),
            ("managed_path", "/server/path", "LOCAL_CAPTURE_MANIFEST_INVALID"),
        ):
            manifest = json.loads(json.dumps(self.manifest))
            manifest[field] = value
            self.write_bundle(manifest=manifest)
            self.assert_code(code, lambda: validate_local_capture_zip(self.archive))
        manifest = json.loads(json.dumps(self.manifest))
        manifest["project_id"] = "foreign-project"
        self.write_bundle(manifest=manifest)
        self.assert_code("LOCAL_CAPTURE_IDENTITY_MISMATCH", lambda: LocalCapturePackageImporter(self.root / "identity-state").import_zip(self.archive, project_id="local-default"))

    def test_invalid_json_values_and_manifest_field_types_return_stable_errors(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["quality"]["ratio"] = float("nan")
        self.write_bundle(manifest=manifest)
        self.assert_code("LOCAL_CAPTURE_MANIFEST_INVALID", lambda: validate_local_capture_zip(self.archive))
        manifest = json.loads(json.dumps(self.manifest))
        manifest["files"][0]["step_id"] = 0
        self.write_bundle(manifest=manifest)
        self.assert_code("LOCAL_CAPTURE_MANIFEST_INVALID", lambda: validate_local_capture_zip(self.archive))
        self.write_bundle()
        self.assert_code("LOCAL_CAPTURE_ARCHIVE_SIZE_INVALID", lambda: validate_local_capture_zip(
            self.archive, expected_size_bytes="12"))

    def test_duplicate_json_keys_are_rejected(self):
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", '{"schema_version":"scenario_capture_manifest/v1","schema_version":"other/v1"}')
        self.assert_code("LOCAL_CAPTURE_MANIFEST_INVALID", lambda: validate_local_capture_zip(self.archive))

    def test_member_count_and_sizes_use_configured_bounds(self):
        limits = LocalCapturePackageLimits(max_archive_bytes=4096, max_total_uncompressed_bytes=256,
                                           max_member_bytes=128, max_manifest_bytes=2048, max_members=3,
                                           max_steps=10, max_json_nodes=1000, max_json_depth=20)
        self.assert_code("LOCAL_CAPTURE_MEMBER_LIMIT", lambda: validate_local_capture_zip(self.archive, limits=limits))
        limits = LocalCapturePackageLimits(max_archive_bytes=4096, max_total_uncompressed_bytes=4096,
                                           max_member_bytes=8, max_manifest_bytes=2048, max_members=20,
                                           max_steps=10, max_json_nodes=1000, max_json_depth=20)
        self.assert_code("LOCAL_CAPTURE_MEMBER_TOO_LARGE", lambda: validate_local_capture_zip(self.archive, limits=limits))

    def test_case_collisions_and_duplicate_manifest_paths_are_rejected(self):
        self.write_bundle(extras={"07_EDIT/TRACE.FTRACE": b"collision"})
        self.assert_code("LOCAL_CAPTURE_FILE_DUPLICATE", lambda: validate_local_capture_zip(self.archive))
        manifest = json.loads(json.dumps(self.manifest))
        manifest["files"].append(dict(manifest["files"][0]))
        self.write_bundle(manifest=manifest)
        self.assert_code("LOCAL_CAPTURE_FILE_DUPLICATE", lambda: validate_local_capture_zip(self.archive))

    def test_platform_control_paths_and_file_directory_conflicts_are_rejected(self):
        for reserved in ("run.json", "RUN.JSON/evidence.bin", "manifest.json/evidence.bin"):
            content = b"reserved"
            manifest = json.loads(json.dumps(self.manifest))
            manifest["files"] = [{
                "step_id": "step_07", "kind": "trace", "name": Path(reserved).name,
                "relative_path": reserved, "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }]
            self.write_bundle(manifest=manifest, files={reserved: content})
            self.assert_code("LOCAL_CAPTURE_PATH_RESERVED", lambda: validate_local_capture_zip(self.archive))

        contents = {"Evidence": b"file", "evidence/trace.ftrace": b"nested"}
        manifest = json.loads(json.dumps(self.manifest))
        manifest["files"] = [{
            "step_id": "step_07", "kind": "trace", "name": Path(relative).name,
            "relative_path": relative, "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        } for relative, content in contents.items()]
        self.write_bundle(manifest=manifest, files=contents)
        self.assert_code("LOCAL_CAPTURE_PATH_CONFLICT", lambda: validate_local_capture_zip(self.archive))

    def test_existing_tampered_run_is_not_treated_as_idempotent(self):
        importer = LocalCapturePackageImporter(self.root / "state")
        importer.import_zip(self.archive, project_id="local-default")
        trace = self.root / "state" / "scenario-capture" / "runs" / self.run_id / "07_edit" / "trace.ftrace"
        trace.write_bytes(b"tampered")
        self.assert_code("LOCAL_CAPTURE_EXISTING_CORRUPT", lambda: importer.import_zip(self.archive, project_id="local-default"))

    def test_extraction_failure_leaves_no_visible_run(self):
        importer = LocalCapturePackageImporter(self.root / "state")
        with patch.object(importer, "_extract", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                importer.import_zip(self.archive, project_id="local-default")
        destination = self.root / "state" / "scenario-capture" / "runs" / self.run_id
        self.assertFalse(destination.exists())
        leftovers = [path for path in destination.parent.iterdir() if path.name.startswith(f".{self.run_id}.")]
        self.assertEqual(leftovers, [])

    def test_expected_archive_hash_and_size_are_checked(self):
        importer = LocalCapturePackageImporter(self.root / "state")
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        result = importer.import_zip(self.archive, project_id="local-default", expected_sha256=digest,
                                     expected_size_bytes=self.archive.stat().st_size)
        self.assertEqual(result["status"], "imported")
        self.assert_code("LOCAL_CAPTURE_ARCHIVE_HASH_MISMATCH", lambda: LocalCapturePackageImporter(self.root / "other").import_zip(
            self.archive, project_id="local-default", expected_sha256="0" * 64))


if __name__ == "__main__":
    unittest.main()
