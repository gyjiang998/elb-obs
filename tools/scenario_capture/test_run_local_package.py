from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import zipfile
import subprocess
import sys

from backend.local_capture_package import LocalCapturePackageImporter, validate_local_capture_zip
from backend.scenario_capture_local_package import build_local_capture_package
from tools.scenario_capture import run_local_package as local_runner


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class LocalPackageRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = self.root / "package"
        self.output = self.root / "output"
        self.package.mkdir()
        self.hdc = self.root / "hdc.exe"
        self.hdc.write_bytes(b"fixed fake hdc")
        self.plan = {
            "artifact_id": "art_0123456789abcdef01234567",
            "plan_id": "art_0123456789abcdef01234567",
            "project_id": "project-test",
            "title": "Local package contract test",
            "profile": {"app_bundle": "com.huawei.hmos.photos"},
            "steps": [{"step_id": "step_01", "title": "Open gallery"}],
            "capture": {
                "selected_step_ids": ["step_01"],
                "collectors": ["trace"],
                "pmu": {"group": "core"},
            },
            "readiness": {"blockers": []},
        }
        write_json(self.package / "plan.json", self.plan)
        digest = hashlib.sha256((self.package / "plan.json").read_bytes()).hexdigest()
        write_json(self.package / "package.json", {
            "schema_version": local_runner.PACKAGE_SCHEMA,
            "package_id": "localcap_0123456789abcdef01234567",
            "project_id": "project-test",
            "plan_id": self.plan["plan_id"],
            "plan_sha256": digest,
            "selected_target": {
                "connection_mode": "bridge",
                "bridge_id": "bridge-test",
                "device_serial": "SERIAL-001",
            },
            "pmu_bundle_path": "pmu-sampler",
            "files": [{
                "path": "plan.json",
                "size_bytes": (self.package / "plan.json").stat().st_size,
                "sha256": digest,
            }],
        })

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def fake_prepare(plan, _client, *, bundle_root, progress):
        record = {"schema_version": "pmu_sampler_preparation/v1", "status": "not_requested"}
        progress(record)
        return plan, record

    @staticmethod
    def fake_execute(plan, _client, run_dir, run_id, progress, *, terminal_run_ids, private_device=False):
        trace = run_dir / "steps" / "step_01" / "trace.ftrace"
        trace.parent.mkdir(parents=True)
        trace.write_bytes(b"trace payload\n")
        manifest = {
            "schema_version": "scenario_capture_manifest/v1",
            "run_id": run_id,
            "plan_id": plan["artifact_id"],
            "status": "succeeded",
            "created_at": "2026-10-26T00:00:00+00:00",
            "steps": [{"step_id": "step_01", "status": "succeeded"}],
            "files": [{
                "relative_path": "steps/step_01/trace.ftrace",
                "name": "trace.ftrace",
                "kind": "trace",
                "step_id": "step_01",
                "size_bytes": trace.stat().st_size,
                "sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            }],
        }
        progress(manifest)
        return manifest

    def run_success(self):
        with (
            mock.patch.object(local_runner.DirectHdcDeviceClient, "verify_connected",
                              return_value={"device_serial": "SERIAL-001", "targets": ["SERIAL-001"]}),
            mock.patch.object(local_runner.DirectHdcDeviceClient, "hdc_version", return_value="fake 1.0"),
        ):
            return local_runner.run_package(
                self.package,
                self.hdc,
                "",
                self.output,
                prepare=self.fake_prepare,
                execute=self.fake_execute,
            )

    def test_authenticates_plan_and_rejects_tampering(self) -> None:
        package_root, package, plan = local_runner.load_package(self.package)
        self.assertEqual(package_root, self.package.resolve())
        self.assertEqual(package["package_id"], "localcap_0123456789abcdef01234567")
        self.assertEqual(plan["artifact_id"], self.plan["artifact_id"])
        (self.package / "plan.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(local_runner.LocalPackageError, "changed|does not match"):
            local_runner.load_package(self.package)

    def test_archive_is_minimal_portable_and_accepted_by_importer(self) -> None:
        result = self.run_success()
        archive = Path(result["archive"])
        validated = validate_local_capture_zip(archive)
        self.assertEqual(validated.manifest["run_id"], result["run_id"])
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(
                sorted(bundle.namelist()),
                ["manifest.json", "steps/step_01/trace.ftrace"],
            )
            portable = json.loads(bundle.read("manifest.json"))
        self.assertNotIn("managed_path", portable)
        self.assertNotIn("managed_path", portable["files"][0])
        self.assertNotIn("storage_locations", portable["files"][0])
        self.assertEqual(portable["device_serial"], "SERIAL-001")
        self.assertEqual(portable["local_execution"]["package_id"], "localcap_0123456789abcdef01234567")

        imported = LocalCapturePackageImporter(self.root / "server-state").import_zip(
            archive,
            project_id="project-test",
        )
        self.assertEqual(imported["status"], "imported")
        self.assertEqual(imported["run_id"], result["run_id"])
        self.assertTrue(
            (self.root / "server-state" / "scenario-capture" / "runs" /
             result["run_id"] / "steps" / "step_01" / "trace.ftrace").is_file()
        )

        local_manifest = json.loads((Path(result["run_directory"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("managed_path", local_manifest["files"][0])
        self.assertEqual(local_manifest["project_id"], "project-test")

    def test_local_runner_supplies_same_device_terminal_runs_for_collector_recovery(self) -> None:
        previous_id = "capture_" + "a" * 24
        other_id = "capture_" + "b" * 24
        for run_id, serial in ((previous_id, "SERIAL-001"), (other_id, "OTHER-DEVICE")):
            write_json(self.output / "scenario-capture" / "runs" / run_id / "run.json", {
                "schema_version": "scenario_capture_run/v1", "run_id": run_id,
                "project_id": "project-test", "connection_mode": "local",
                "device_serial": serial, "status": "failed",
            })
        observed = []

        def execute(plan, client, run_dir, run_id, progress, *, terminal_run_ids, private_device):
            observed.append(set(terminal_run_ids))
            self.assertFalse(private_device)
            return self.fake_execute(plan, client, run_dir, run_id, progress,
                                     terminal_run_ids=terminal_run_ids)

        with (
            mock.patch.object(local_runner.DirectHdcDeviceClient, "verify_connected",
                              return_value={"device_serial": "SERIAL-001", "targets": ["SERIAL-001"]}),
            mock.patch.object(local_runner.DirectHdcDeviceClient, "hdc_version", return_value="fake 1.0"),
        ):
            result = local_runner.run_package(self.package, self.hdc, "", self.output,
                prepare=self.fake_prepare, execute=execute)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(observed, [{previous_id}])

    def test_preparation_failure_still_emits_importable_terminal_archive(self) -> None:
        with mock.patch.object(
            local_runner.DirectHdcDeviceClient,
            "verify_connected",
            side_effect=local_runner.LocalPackageError("selected device is offline"),
        ):
            result = local_runner.run_package(
                self.package,
                self.hdc,
                "",
                self.output,
                prepare=self.fake_prepare,
                execute=self.fake_execute,
            )
        self.assertEqual(result["status"], "failed")
        validated = validate_local_capture_zip(Path(result["archive"]))
        self.assertEqual(validated.manifest["status"], "failed")
        self.assertEqual(validated.manifest["files"][0]["relative_path"], "local-execution.json")

    def test_exact_device_selection_has_no_implicit_fallback(self) -> None:
        run_dir = self.root / "jobs"
        run_dir.mkdir()
        client = local_runner.DirectHdcDeviceClient(self.hdc, "SERIAL-001", run_dir, threading.Event())
        with mock.patch.object(client, "_run", return_value={"stdout": "OTHER-DEVICE\n"}) as invoke:
            with self.assertRaisesRegex(local_runner.LocalPackageError, "not present"):
                client.verify_connected()
        invoke.assert_called_once_with("device.discover", ["list", "targets"], timeout=20, targeted=False)

    def test_unbound_package_auto_selects_unique_local_device_and_records_it(self) -> None:
        metadata_path = self.package / "package.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["selected_target"]["device_serial"] = ""
        write_json(metadata_path, metadata)
        with (
            mock.patch.object(local_runner.DirectHdcDeviceClient, "_run",
                              return_value={"stdout": "[Info] daemon running\nSERIAL-002\nSERIAL-002\n"}) as invoke,
            mock.patch.object(local_runner.DirectHdcDeviceClient, "hdc_version", return_value="fake 1.0"),
        ):
            result = local_runner.run_package(self.package, self.hdc, "", self.output,
                prepare=self.fake_prepare, execute=self.fake_execute)
        invoke.assert_called_once_with("device.discover", ["list", "targets"], timeout=20, targeted=False)
        self.assertEqual(result["device_serial"], "SERIAL-002")
        self.assertEqual(result["status"], "succeeded")
        run = json.loads((Path(result["run_directory"]) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["device"]["serial"], "SERIAL-002")
        self.assertEqual(validate_local_capture_zip(Path(result["archive"])).manifest["device_serial"], "SERIAL-002")

    def test_unbound_package_requires_explicit_selection_with_multiple_local_devices(self) -> None:
        run_dir = self.root / "multi-jobs"
        run_dir.mkdir()
        client = local_runner.DirectHdcDeviceClient(self.hdc, "", run_dir, threading.Event())
        with mock.patch.object(client, "_run", return_value={"stdout": "SERIAL-002\nSERIAL-001\n"}):
            with self.assertRaisesRegex(local_runner.LocalPackageError, "Multiple local HDC devices") as caught:
                client.verify_connected()
        self.assertEqual(client.serial, "")
        self.assertIn('.\\run-capture.ps1 -DeviceSerial "SERIAL-001"', str(caught.exception))
        self.assertIn('.\\run-capture.ps1 -DeviceSerial "SERIAL-002"', str(caught.exception))

    def test_unbound_package_reports_no_device_and_blocks_targeted_calls(self) -> None:
        run_dir = self.root / "empty-jobs"
        run_dir.mkdir()
        client = local_runner.DirectHdcDeviceClient(self.hdc, "", run_dir, threading.Event())
        with mock.patch.object(client, "_run", return_value={"stdout": "[Empty]\n"}):
            with self.assertRaisesRegex(local_runner.LocalPackageError, "No local HDC device.*hdc list targets"):
                client.verify_connected()
        with mock.patch.object(local_runner.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(local_runner.LocalPackageError, "valid HDC device serial"):
                client._run("device.execute_shell", ["shell", "true"], timeout=2)
        spawn.assert_not_called()

    def test_explicit_override_preserves_exact_binding_over_package_default(self) -> None:
        _, metadata, _ = local_runner.load_package(self.package)
        self.assertEqual(local_runner._target_from_package(metadata, "SERIAL-OVERRIDE"), "SERIAL-OVERRIDE")
        self.assertEqual(local_runner._target_from_package(metadata, ""), "SERIAL-001")
        with self.assertRaises(local_runner.LocalPackageError):
            local_runner._target_from_package(metadata, "invalid serial")

    def test_cancelled_capture_keeps_independent_cleanup_channel(self) -> None:
        run_dir = self.root / "cleanup-jobs"
        run_dir.mkdir()
        cancelled = threading.Event()
        cancelled.set()
        client = local_runner.DirectHdcDeviceClient(Path(sys.executable), "SERIAL-001", run_dir, cancelled)
        with self.assertRaises(local_runner.CaptureCancelled):
            client._run("workload", ["-c", "print('must not run')"], timeout=2, targeted=False)
        result = client._run(
            "device.execute_shell.cleanup",
            ["-c", "print('cleanup completed')"],
            timeout=2,
            targeted=False,
            cleanup=True,
        )
        self.assertEqual(result["stdout"], "cleanup completed")
        self.assertEqual(client.jobs[-1]["status"], "succeeded")
        self.assertTrue(client.jobs[-1]["cleanup"])

    def test_collected_artifact_publication_is_concurrent_and_idempotent(self) -> None:
        destination = self.root / "collected" / "layout.json"
        barrier = threading.Barrier(2)
        clients = [local_runner.DirectHdcDeviceClient(self.hdc, "SERIAL-001", self.root / f"jobs-{index}", threading.Event())
                   for index in range(2)]
        for index, client in enumerate(clients):
            client.jobs = [{"job_id": f"job-{index}"}]

            def receive(_action, args, *, timeout, cleanup=False, payload=b'{"page":"gallery"}', **_kwargs):
                Path(args[-1]).write_bytes(payload)
                barrier.wait(timeout=2)
                return {"duration_ms": 1}

            client._run = receive

        results: list[dict] = []
        failures: list[Exception] = []

        def collect(client):
            try:
                results.append(client.collect("/data/local/tmp/layout.json", destination))
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        workers = [threading.Thread(target=collect, args=(client,)) for client in clients]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=3)
        self.assertEqual(failures, [])
        self.assertEqual(destination.read_bytes(), b'{"page":"gallery"}')
        self.assertEqual(sorted(item["publication"] for item in results), ["created", "idempotent_reuse"])
        self.assertFalse(any(destination.parent.glob(".*.recv")))

    def test_collected_artifact_replay_never_overwrites_different_content(self) -> None:
        destination = self.root / "collected" / "layout.json"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"first")
        client = local_runner.DirectHdcDeviceClient(self.hdc, "SERIAL-001", self.root / "jobs", threading.Event())
        client.jobs = [{"job_id": "job-conflict"}]

        def receive(_action, args, *, timeout, cleanup=False, **_kwargs):
            Path(args[-1]).write_bytes(b"second")
            return {"duration_ms": 1}

        client._run = receive
        with self.assertRaisesRegex(local_runner.LocalPackageError, "different data"):
            client.collect("/data/local/tmp/layout.json", destination)
        self.assertEqual(destination.read_bytes(), b"first")

    def test_external_cancel_event_and_progress_preserve_terminal_evidence(self) -> None:
        cancellation = threading.Event()
        updates = []

        def cancelled_execute(plan, client, run_dir, run_id, progress, *, terminal_run_ids, private_device):
            cancellation.set()
            # The same event is visible to the fixed Runner's client; teardown
            # uses the independent cleanup path tested above.
            client.check_cancelled()

        with (mock.patch.object(local_runner.DirectHdcDeviceClient, "verify_connected", return_value={"targets": ["SERIAL-001"]}),
              mock.patch.object(local_runner.DirectHdcDeviceClient, "hdc_version", return_value="fake")):
            result = local_runner.run_package(self.package, self.hdc, "SERIAL-001", self.output,
                prepare=self.fake_prepare, execute=cancelled_execute, cancel_event=cancellation,
                progress_callback=updates.append, install_signal_handlers=False)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(updates[0]["stage"], "device_preparation")
        self.assertEqual(updates[-1]["stage"], "finished")
        self.assertEqual(updates[-1]["status"], "cancelled")
        self.assertTrue(Path(result["manifest_path"]).is_file())
        self.assertTrue(Path(result["run_path"]).is_file())
        validated = validate_local_capture_zip(Path(result["archive"]))
        self.assertEqual(validated.manifest["status"], "cancelled")

    def test_powershell_entry_is_package_self_contained(self) -> None:
        script = (Path(__file__).resolve().parents[2] / "deploy" / "windows" /
                  "run-local-scenario-capture.ps1").read_text(encoding="utf-8")
        self.assertIn('[Parameter(Position = 0)][string]$Package = ""', script)
        self.assertIn('$Package = $PSScriptRoot', script)
        self.assertIn('runtime\\tools\\scenario_capture\\run_local_package.py', script)
        self.assertIn('sys.version_info >= (3, 11)', script)
        self.assertNotIn("Invoke-Expression", script)

    def test_generated_runtime_handles_pmu_error_without_server_backend(self) -> None:
        built = build_local_capture_package(
            plan=self.plan,
            project_id="project-test",
            plan_id=self.plan["plan_id"],
            device_serial="SERIAL-001",
            bridge_id="bridge-test",
        )
        package_zip = self.root / "generated.zip"
        package_zip.write_bytes(built["content"])
        extracted = self.root / "isolated"
        with zipfile.ZipFile(package_zip) as bundle:
            bundle.extractall(extracted)
        entry = extracted / "runtime" / "tools" / "scenario_capture" / "run_local_package.py"
        code = (
            "import importlib.util, os; "
            "s=importlib.util.spec_from_file_location('portable_runner', os.environ['PORTABLE_ENTRY']); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "import backend.pmu_sampler as p; "
            "C=type('C',(),{'check_cancelled':lambda self: None}); "
            "p._propagate_cancellation(C(), RuntimeError('probe failed')); "
            "assert __import__('backend.scenario_capture',fromlist=['x']).CaptureCancelled is m.CaptureCancelled; "
            "print('portable-pmu-ok')"
        )
        environment = {"PORTABLE_ENTRY": str(entry), "PYTHONUTF8": "1"}
        completed = subprocess.run(
            [sys.executable, "-I", "-c", code],
            cwd=self.root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("portable-pmu-ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
