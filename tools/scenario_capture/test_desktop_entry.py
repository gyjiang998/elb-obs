from __future__ import annotations

import hashlib
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from backend.core import PlatformError
from backend import core
from tools.scenario_capture import desktop_entry as desktop
from tools.scenario_capture import run_local_package as runner


class DesktopCaptureEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = {"template_id": "custom", "title": "桌面采集 / 操作窗口",
            "steps": [{"step_id": "step_01", "title": "等待页面稳定", "operation": "wait", "seconds": 1}],
            "capture": {"selected_step_ids": ["step_01"], "collectors": ["trace"]}}

    def save(self, payload=None):
        return desktop.save_plan(payload or self.payload, self.root / "plans")

    def make_trace(self):
        root = self.root / "trace-source"
        root.mkdir()
        (root / "trace.ftrace").write_bytes(b"trace evidence\n")
        entry = {"relative_path": "trace.ftrace", "name": "trace.ftrace", "kind": "trace",
            "step_id": "step_01", "size_bytes": 15,
            "sha256": hashlib.sha256(b"trace evidence\n").hexdigest()}
        # Derive the actual length; the fixture is portable across line endings.
        entry["size_bytes"] = (root / "trace.ftrace").stat().st_size
        manifest = {"schema_version": "scenario_capture_manifest/v1", "run_id": "capture_" + "a" * 24,
            "plan_id": "art_" + "b" * 24, "status": "succeeded", "project_id": "desktop-local",
            "device_serial": "SERIAL-TEST", "steps": [{"step_id": "step_01", "status": "succeeded"}],
            "files": [entry]}
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        archive = self.root / "sample.capture.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest))
            bundle.write(root / "trace.ftrace", "trace.ftrace")
        return root, archive, manifest

    def test_catalog_uses_desktop_10us_with_all_event_groups(self):
        value = desktop.catalog()
        self.assertEqual(value["default_capture"]["pmu"]["period_us"], 10)
        self.assertEqual(value["default_capture"]["pmu"]["group"], "auto")
        self.assertEqual(len(value["templates"][0]["steps"]), 17)
        self.assertEqual(value["pmu_sampler"]["platform_bundle"]["status"], "ready")
        self.assertEqual(value["pmu_sampler"]["auto_event_count"], 14)

    def test_saved_packages_are_immutable_and_use_shared_plan_contract(self):
        first, second = self.save(), self.save()
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertNotEqual(first["package_directory"], second["package_directory"])
        package, metadata, plan = runner.load_package(Path(first["package_directory"]))
        self.assertEqual(plan["capture"]["pmu"]["period_us"], 10)
        self.assertEqual(plan["execution_step_ids"], ["step_01"])
        self.assertEqual(plan["readiness"]["status"], "ready_for_device_preflight")
        self.assertEqual(metadata["selected_target"]["device_serial"], "")
        self.assertTrue((package / "runtime/tools/scenario_capture/runner.py").is_file())
        self.assertTrue((package / "pmu-sampler/manifest.json").is_file())

    def test_saved_explicit_period_is_not_replaced_by_desktop_default(self):
        self.payload["capture"]["pmu"] = {"period_us": 100}
        self.assertEqual(self.save()["plan"]["capture"]["pmu"]["period_us"], 100)

    def test_windows_save_plan_completes_artifact_event_and_package_on_every_save(self):
        native_open = os.open

        def windows_open(path, flags, *args, **kwargs):
            if Path(path).is_dir():
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return native_open(path, flags, *args, **kwargs)

        with patch.object(core, "sys", SimpleNamespace(platform="win32")), \
             patch.object(core.os, "open", side_effect=windows_open):
            first, repeated = self.save(), self.save()
            changed = self.save({**self.payload, "title": "更新后的采集计划"})

        self.assertEqual(first["plan_id"], repeated["plan_id"])
        self.assertNotEqual(first["plan_id"], changed["plan_id"])
        for saved in (first, repeated, changed):
            _, metadata, plan = runner.load_package(Path(saved["package_directory"]))
            self.assertEqual(plan["artifact_id"], saved["plan_id"])
            self.assertEqual(metadata["plan_id"], saved["plan_id"])
            self.assertTrue(Path(saved["package_zip"]).is_file())
        events_path = self.root / "plans/plan-store/events/analysis_events.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        self.assertEqual([event["artifact_id"] for event in events], [first["plan_id"], changed["plan_id"]])
        self.assertFalse(any(path.name.endswith((".tmp", ".partial")) or path.name.startswith(".package-")
                             for path in (self.root / "plans").rglob("*")))

    def test_replayed_package_publication_reuses_identical_directory_and_archive(self):
        first = self.save()
        content = Path(first["package_zip"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            metadata = json.loads(bundle.read("package.json"))
        built = {"content": content, "filename": Path(first["package_zip"]).name,
                 "package": metadata, "sha256": hashlib.sha256(content).hexdigest()}
        with patch.object(desktop, "build_local_capture_package", return_value=built):
            repeated = self.save()
        self.assertEqual(repeated["package_directory"], first["package_directory"])
        self.assertEqual(repeated["package_zip"], first["package_zip"])
        self.assertEqual(Path(repeated["package_zip"]).read_bytes(), content)
        self.assertFalse(any(path.name.endswith(".partial") or path.name.startswith(".package-")
                             for path in (self.root / "plans").rglob("*")))

    def test_unresolved_steps_are_saved_but_package_run_remains_blocked(self):
        self.payload["steps"] = ["打开图库"]
        result = self.save()
        self.assertEqual(result["readiness"]["status"], "needs_input")
        with self.assertRaisesRegex(runner.LocalPackageError, "unresolved"):
            runner.load_package(Path(result["package_directory"]))

    def test_existing_shared_validation_rejects_bad_plan_fields(self):
        for change in ({"steps": [{"operation": "wait", "title": "x", "command": "reboot"}]},
                       {"capture": {"selected_step_ids": ["missing"]}},
                       {"capture": {"pmu": {"period_us": 1}}}):
            with self.subTest(change=change), self.assertRaises(PlatformError):
                self.save({**self.payload, **change})

    def test_directory_and_archive_register_same_evidence_identity(self):
        root, archive, manifest = self.make_trace()
        # Local storage annotations do not alter the portable evidence identity.
        manifest["files"][0]["managed_path"] = "scenario-capture/runs/x/trace.ftrace"
        manifest["files"][0]["storage_locations"] = [{"kind": "windows_local"}]
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        directory_result = desktop.inspect_trace(root)
        archive_result = desktop.inspect_trace(archive)
        self.assertEqual(directory_result["identity_sha256"], archive_result["identity_sha256"])
        self.assertTrue(directory_result["verified"])
        self.assertEqual(directory_result["files"][0]["absolute_path"], str((root / "trace.ftrace").resolve()))
        self.assertNotIn("absolute_path", archive_result["files"][0])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["sample.capture.zip", "trace-source"])

    def test_inspection_hashes_actual_zip_members_and_directory_files(self):
        root, archive, manifest = self.make_trace()
        (root / "trace.ftrace").write_bytes(b"bad evidence!!\n")
        with self.assertRaises(PlatformError):
            desktop.inspect_trace(root)
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest))
            bundle.writestr("trace.ftrace", b"bad evidence!!\n")
        with self.assertRaises(PlatformError):
            desktop.inspect_trace(archive)

    def test_directory_inspection_rejects_path_escape_and_symlink(self):
        root, _, manifest = self.make_trace()
        manifest["files"][0]["relative_path"] = "../outside.ftrace"
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(PlatformError):
            desktop.inspect_trace(root)
        manifest["files"][0]["relative_path"] = "trace.ftrace"
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        target = root / "trace.ftrace"
        target.unlink()
        outside = self.root / "outside.ftrace"
        outside.write_bytes(b"trace evidence\n")
        try:
            target.symlink_to(outside)
        except OSError:
            return  # Windows may require developer mode to create symlinks.
        with self.assertRaises(PlatformError):
            desktop.inspect_trace(root)

    def test_run_requires_explicit_device(self):
        with patch.object(runner, "run_package") as execute, self.assertRaises(runner.LocalPackageError):
            desktop.run_capture(Path("package"), Path("hdc"), "", self.root)
        execute.assert_not_called()

    def test_runner_failure_without_files_keeps_directory_and_zip_equivalent(self):
        package = Path(self.save()["package_directory"])
        for status in ("failed", "cancelled", "cancel_after_progress"):
            with self.subTest(status=status):
                def prepare(plan, client, **kwargs):
                    return plan, {"status": "not_requested"}

                def execute(plan, client, run_dir, run_id, progress, **kwargs):
                    manifest = {"schema_version": "scenario_capture_manifest/v1", "run_id": run_id,
                        "plan_id": plan["artifact_id"], "status": "running" if status == "cancel_after_progress" else status,
                        "steps": [], "files": [], "quality": {"errors": []}}
                    progress(manifest)
                    if status == "cancel_after_progress":
                        raise runner.CaptureCancelled("cancelled before first evidence file")
                    return manifest

                with patch.object(runner.DirectHdcDeviceClient, "verify_connected",
                                  return_value={"targets": ["SERIAL-TEST"]}), \
                     patch.object(runner.DirectHdcDeviceClient, "hdc_version", return_value="test"):
                    result = runner.run_package(package, Path(sys.executable), "SERIAL-TEST",
                        self.root / "runs", prepare=prepare, execute=execute)
                directory = desktop.inspect_trace(Path(result["run_directory"]))
                archive = desktop.inspect_trace(Path(result["archive"]))
                self.assertEqual(directory["identity_sha256"], archive["identity_sha256"])
                self.assertEqual(directory["manifest"]["status"], result["status"])
                self.assertEqual(directory["files"][0]["relative_path"], "local-execution.json")

    def test_cancel_file_sets_event_and_joins_monitor(self):
        control = self.root / "cancel"

        def fake_run(*args, cancel_event, progress_callback, explicitly_terminal_run_ids, private_device):
            self.assertEqual(explicitly_terminal_run_ids, {"capture_" + "a" * 24})
            self.assertTrue(private_device)
            control.write_text("cancel")
            self.assertTrue(cancel_event.wait(2))
            return {"status": "cancelled"}

        with patch.object(runner, "run_package", side_effect=fake_run):
            result = desktop.run_capture(Path("package"), Path("hdc"), "SERIAL-TEST", self.root, cancel_file=control,
                recover_run_ids={"capture_" + "a" * 24})
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(any(thread.name == "desktop-capture-cancel" for thread in threading.enumerate()))

    def test_lost_parent_requests_cooperative_cancellation(self):
        def fake_run(*args, cancel_event, progress_callback, explicitly_terminal_run_ids, private_device):
            self.assertEqual(explicitly_terminal_run_ids, None)
            self.assertTrue(private_device)
            self.assertTrue(cancel_event.wait(2))
            return {"status": "cancelled"}

        with patch.object(runner, "run_package", side_effect=fake_run), patch.object(desktop, "_parent_alive", return_value=False):
            result = desktop.run_capture(Path("package"), Path("hdc"), "SERIAL-TEST", self.root, parent_pid=99)
        self.assertEqual(result["status"], "cancelled")

    def test_entry_runs_without_site_packages_and_emits_only_ndjson(self):
        entry = Path(desktop.__file__)
        result = subprocess.run([sys.executable, "-I", "-S", str(entry), "catalog"],
            cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "result")
        self.assertEqual(rows[0]["default_capture"]["pmu"]["period_us"], 10)
        invalid = self.root / "invalid.json"
        invalid.write_text('{"template_id":"unknown"}', encoding="utf-8")
        failure = subprocess.run([sys.executable, "-I", "-S", str(entry), "save-plan", "--input", str(invalid),
            "--output-root", str(self.root / "saved")], cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(failure.returncode, 2)
        self.assertEqual(json.loads(failure.stdout)["type"], "error")
        self.assertEqual(json.loads(failure.stdout)["code"], "SCENARIO_PLAN_INVALID")

    def test_real_subprocess_hdc_failure_leaves_inspectable_directory_and_zip(self):
        saved = self.save()
        result = subprocess.run([sys.executable, "-I", "-S", str(Path(desktop.__file__)), "run",
            "--package", saved["package_directory"], "--hdc", sys.executable,
            "--device-serial", "SERIAL-TEST", "--output-root", str(self.root / "results"),
            "--parent-pid", str(os.getpid())], cwd=self.root, capture_output=True,
            text=True, encoding="utf-8", timeout=20)
        # Python is an existing binary but does not implement HDC argv. This
        # exercises the actual child-process failure without contacting a phone.
        self.assertEqual(result.returncode, 1, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(rows[0]["type"], "progress")
        terminal = rows[-1]
        self.assertEqual(terminal["type"], "result")
        self.assertEqual(terminal["status"], "failed")
        self.assertIn("Local HDC", terminal["error"]["message"])
        archive = desktop.inspect_trace(Path(terminal["archive"]))
        directory = desktop.inspect_trace(Path(terminal["run_directory"]))
        self.assertEqual(archive["identity_sha256"], directory["identity_sha256"])


if __name__ == "__main__":
    unittest.main()
