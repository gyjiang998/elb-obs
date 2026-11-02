"""PMU preparation tests use only protocol doubles, never host HDC or a phone."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.core import ArtifactStore
from backend.pmu_sampler import DEFAULT_DEVICE_PATH, LEGACY_DEVICE_PATH, _probe, platform_bundle_status, prepare_pmu_sampler
from backend.scenario_capture import CaptureCancelled, GatewayDeviceClient, LocalDeviceClient, ScenarioCaptureManager
from backend.tests.test_scenario_capture import SimulatedDeviceClient

DIGEST = "a" * 64
CAPS = {"schema_version": "gallery_thread_pmu_capabilities/v1", "sampler_version": "1.1.0",
        "period_us_min": 10, "period_us_max": 10000, "event_groups": ["cache"]}


def plan(path=DEFAULT_DEVICE_PATH, collectors=None, period=10):
    return {"capture": {"collectors": collectors if collectors is not None else ["trace", "pmu"],
        "pmu": {"binary_path": path, "period_us": period, "group": "cache"}}}


class Device:
    def __init__(self, availability="executable", compatibility="supported", caps=None):
        self.availability, self.compatibility = availability, compatibility
        self.caps = CAPS if caps is None else caps
        self.calls = []
        self.deployments = []
        self.probe_timeout = False
        self.error = None
        self.cancelled = False
        self.installed_paths = set()
        self.managed_hash = DIGEST

    def check_cancelled(self):
        if self.cancelled:
            raise CaptureCancelled("cancelled")

    def shell(self, command, timeout=40, *, cleanup=False):
        self.calls.append((command, timeout, cleanup))
        self.check_cancelled()
        if self.error:
            raise self.error
        if command.startswith("if [ -f "):
            if shlex.split(command)[3] in self.installed_paths:
                return "executable"
            return self.availability
        if command.startswith("(sha256sum "):
            return self.managed_hash + "  /data/local/tmp/managed_sampler"
        return "\n".join(["__PMU_COMPAT__", json.dumps({"status": self.compatibility}),
            "__PMU_PROBE__", json.dumps({"timed_out": self.probe_timeout, "exit_code": 0}),
            "__PMU_CAPS__", json.dumps(self.caps), "__PMU_HASH__", DIGEST + "  /data/local/tmp/sampler",
            "__PMU_STDERR__", "", "__PMU_END__"])

    def deploy(self, local_file, remote_path, sha256, *, timeout):
        self.check_cancelled()
        self.deployments.append((local_file, remote_path, sha256))
        self.installed_paths.add(remote_path)
        return {"status": "succeeded", "deployment_verified": True, "remote_sha256": sha256, "job_id": "device_test"}


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = {"binary_path": self.root / "sampler", "binary_sha256": DIGEST,
            "source_sha256": "b" * 64, "sampler_version": "1.1.0", "target": "aarch64-linux-ohos", "receipt_path": self.root / "receipt.json"}

    def test_catalog_bundle_status_is_readonly_and_never_contacts_device(self):
        gateway = Mock()
        manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), gateway)
        before = sorted(str(item) for item in self.root.rglob("*"))
        with patch("backend.pmu_sampler.sampler_bundle_root", return_value=self.root / "absent"):
            with patch("backend.scenario_capture.resolve_hdc_binary", return_value=self.root / "hdc"):
                catalog = manager.catalog()
        missing = catalog["pmu_sampler"]["platform_bundle"]
        self.assertEqual(missing["status"], "missing")
        self.assertEqual(missing["device_status"], "not_probed")
        self.assertEqual(catalog["default_capture"]["pmu"]["binary_path"], DEFAULT_DEVICE_PATH)
        self.assertEqual(catalog["pmu_sampler"]["source_asset"], "tools/scenario_capture/thread_pmu_sampler.c")
        self.assertEqual(catalog["pmu_sampler"]["display_name"], "通用线程 PMU 采样器")
        self.assertTrue(catalog["pmu_sampler"]["user_only"])
        self.assertEqual(catalog["pmu_sampler"]["scope"], "target_main_thread")
        self.assertIn("provision_pmu_sampler", missing["remediation"]["build_command"])
        self.assertEqual(gateway.mock_calls, [])
        self.assertEqual(before, sorted(str(item) for item in self.root.rglob("*")))
        with patch("tools.scenario_capture.pmu_bundle.verify_bundle", return_value=self.bundle):
            ready = platform_bundle_status(self.root)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["binary_sha256"], DIGEST)
        self.assertNotIn("receipt", ready)
        with patch("tools.scenario_capture.pmu_bundle.verify_bundle", side_effect=ValueError("corrupted")):
            invalid = platform_bundle_status(self.root)
        self.assertEqual(invalid["status"], "invalid")
        self.assertIn("corrupted", invalid["reason"])

    def prepare(self, device, value=None):
        with patch("tools.scenario_capture.pmu_bundle.verify_bundle", return_value=self.bundle):
            return prepare_pmu_sampler(value or plan(), device, bundle_root=self.root)

    def test_unselected_pmu_has_no_device_operations(self):
        device = Device()
        _, record = self.prepare(device, plan(collectors=["trace"]))
        self.assertEqual(record["status"], "not_requested")
        self.assertEqual(device.calls, [])

    def test_auto_upgrades_legacy_default_to_new_hash_without_overwrite_or_plan_mutation(self):
        multi_caps = {**CAPS, "sampler_version": "1.2.0", "event_groups": ["auto", "all", "cache"]}
        self.bundle["sampler_version"] = "1.2.0"
        for old_modes in (["cache"], None):
            device = Device("missing", caps={**CAPS, "event_groups": old_modes})
            device.installed_paths.add(DEFAULT_DEVICE_PATH)
            original_deploy = device.deploy
            def deploy(*args, **kwargs):
                result = original_deploy(*args, **kwargs)
                device.caps = multi_caps
                return result
            device.deploy = deploy
            value = plan(); value["capture"]["pmu"]["group"] = "auto"
            original = copy.deepcopy(value)
            execution, record = self.prepare(device, value)
            self.assertEqual(record["status"], "installed")
            self.assertEqual(record["upgrade_reason"], "multi_group_mode_unavailable")
            self.assertEqual(record["previous_sampler"]["capabilities"]["event_groups"], old_modes)
            self.assertEqual(value, original)
            self.assertTrue(execution["capture"]["pmu"]["preparation"]["allow_execution"])
            self.assertEqual(len(device.deployments), 1)
            self.assertNotEqual(device.deployments[0][1], DEFAULT_DEVICE_PATH)
            self.assertIn(DEFAULT_DEVICE_PATH, device.installed_paths)

    def test_auto_custom_legacy_path_reports_required_upgrade_without_deploying(self):
        device = Device()
        value = plan("/data/local/tmp/custom_sampler"); value["capture"]["pmu"]["group"] = "auto"
        execution, record = self.prepare(device, value)
        self.assertEqual(record["code"], "PMU_MULTI_GROUP_UPGRADE_REQUIRED")
        self.assertFalse(execution["capture"]["pmu"]["preparation"]["allow_execution"])
        self.assertFalse(device.deployments)

    def test_auto_current_default_reuses_one_capability_probe(self):
        device = Device(caps={**CAPS, "sampler_version": "1.2.0", "event_groups": ["auto", "cache"]})
        value = plan(); value["capture"]["pmu"]["group"] = "auto"
        execution, record = self.prepare(device, value)
        self.assertEqual(record["status"], "existing")
        self.assertTrue(execution["capture"]["pmu"]["preparation"]["allow_execution"])
        self.assertEqual(sum("--capabilities" in command for command, *_ in device.calls), 1)
        self.assertFalse(device.deployments)

    def test_probe_accepts_crlf_and_transport_diagnostics_outside_complete_response(self):
        device = Device(caps={**CAPS, "event_groups": ["auto", "all", "cache"]})
        response = device.shell("probe").replace("\n", "\r\n")
        device.shell = Mock(return_value="/bin/sh: tr: inaccessible or not found\r\n" + response
            + "\r\n[W] FreeChannelContinue handle->data is nullptr\r\n")
        observation = _probe(device, DEFAULT_DEVICE_PATH, 100)
        self.assertEqual(observation["capabilities"]["event_groups"], ["auto", "all", "cache"])
        self.assertEqual(observation["device_sha256"], DIGEST)
        self.assertEqual(observation["probe_stderr"], "")
        self.assertIn("tr: inaccessible", observation["shell_diagnostics"])
        self.assertIn("FreeChannelContinue", observation["shell_diagnostics"])

    def test_probe_accepts_empty_sections_after_gateway_removes_blank_lines(self):
        device = Device()
        response = device.shell("probe").replace(json.dumps(CAPS), "").replace(DIGEST + "  /data/local/tmp/sampler", "")
        # The real local gateway removes blank lines; there is only one newline
        # between an empty CAPS/HASH/STDERR section and the next marker.
        device.shell = Mock(return_value="\n".join(line for line in response.splitlines() if line.strip()))
        observation = _probe(device, LEGACY_DEVICE_PATH, 100)
        self.assertIsNone(observation["capabilities"])
        self.assertIsNone(observation["device_sha256"])
        self.assertEqual(observation["probe_stderr"], "")
        self.assertFalse(observation["probe"]["timed_out"])

    def test_incomplete_or_ambiguous_probe_response_reports_evidence_and_blocks_execution(self):
        for mutation, detail in ((lambda value: value.replace("__PMU_END__", ""), "END"),
                                 (lambda value: value + "\n__PMU_PROBE__\n{}", "重复段")):
            device = Device()
            shell = device.shell
            def response(command, timeout=40, **kwargs):
                value = shell(command, timeout=timeout, **kwargs)
                return mutation(value) if "--capabilities" in command else value
            device.shell = response
            value = plan(); value["capture"]["pmu"]["group"] = "auto"
            execution, record = self.prepare(device, value)
            self.assertEqual(record["code"], "PMU_SAMPLER_CAPABILITIES_FAILED")
            self.assertIn(detail, record["reason"])
            self.assertEqual(execution["capture"]["pmu"]["preparation"]["reason"], record["reason"])
            self.assertFalse(execution["capture"]["pmu"]["preparation"]["allow_execution"])
            self.assertFalse(device.deployments)

    def test_legacy_usage_with_shell_diagnostics_still_upgrades_default_sampler(self):
        device = Device("missing", caps={})
        device.installed_paths.add(LEGACY_DEVICE_PATH)
        original_shell, original_deploy = device.shell, device.deploy
        self.bundle["sampler_version"] = "1.2.0"
        def response(command, timeout=40, **kwargs):
            value = original_shell(command, timeout=timeout, **kwargs)
            if "--capabilities" in command:
                value = value.replace('__PMU_STDERR__\n\n', '__PMU_STDERR__\nusage: sampler target_tid ...\n')
                return "/bin/sh: diagnostic from device\n" + value + "\n[W] HDC warning"
            return value
        def deploy(*args, **kwargs):
            result = original_deploy(*args, **kwargs)
            device.caps = {**CAPS, "sampler_version": "1.2.0", "event_groups": ["auto", "all", "cache"]}
            return result
        device.shell, device.deploy = response, deploy
        value = plan(); value["capture"]["pmu"]["group"] = "all"
        execution, record = self.prepare(device, value)
        self.assertEqual(record["status"], "installed")
        self.assertEqual(record["previous_sampler"]["probe_stderr"], "usage: sampler target_tid ...")
        self.assertIn("diagnostic from device", record["previous_sampler"]["shell_diagnostics"])
        self.assertTrue(execution["capture"]["pmu"]["preparation"]["allow_execution"])
        self.assertEqual(len(device.deployments), 1)
        self.assertIn(LEGACY_DEVICE_PATH, device.installed_paths)

    def test_existing_custom_executable_is_reused_without_deployment(self):
        value = plan("/data/local/tmp/custom_sampler")
        device = Device()
        execution, record = self.prepare(device, value)
        self.assertEqual(record["status"], "existing")
        self.assertTrue(execution["capture"]["pmu"].pop("preparation")["allow_execution"])
        self.assertEqual(execution, value)
        self.assertEqual(device.deployments, [])
        self.assertEqual(record["perf_permission"], "not_tested")
        self.assertIn("pmu_probe_ticks=40", device.calls[1][0])
        self.assertIn("stop_pmu_capability_probe", device.calls[1][0])
        self.assertLessEqual(device.calls[1][1], 10)

    def test_legacy_sampler_at_historical_interval_is_kept(self):
        device = Device(compatibility="legacy_unverified", caps={})
        _, record = self.prepare(device, plan(period=100))
        self.assertEqual(record["status"], "existing_legacy_unverified")
        self.assertFalse(device.deployments)

    def test_generic_default_reuses_existing_legacy_path_without_platform_assets(self):
        device = Device("missing")
        device.installed_paths.add(LEGACY_DEVICE_PATH)
        value = plan()
        execution, record = prepare_pmu_sampler(value, device, bundle_root=self.root / "absent")
        self.assertEqual(record["status"], "existing")
        self.assertEqual(record["origin"], "existing_legacy_path")
        self.assertEqual(record["requested_device_path"], DEFAULT_DEVICE_PATH)
        self.assertEqual(execution["capture"]["pmu"]["binary_path"], LEGACY_DEVICE_PATH)
        self.assertEqual(value["capture"]["pmu"]["binary_path"], DEFAULT_DEVICE_PATH)
        self.assertTrue(execution["capture"]["pmu"]["preparation"]["allow_execution"])
        self.assertFalse(device.deployments)

    def test_legacy_path_fallback_still_requires_requested_capabilities(self):
        device = Device("missing", compatibility="period_unsupported")
        device.installed_paths.add(LEGACY_DEVICE_PATH)
        execution, record = self.prepare(device)
        self.assertEqual(record["status"], "incompatible")
        self.assertFalse(execution["capture"]["pmu"]["preparation"]["allow_execution"])
        self.assertEqual(execution["capture"]["pmu"]["binary_path"], DEFAULT_DEVICE_PATH)
        self.assertFalse(device.deployments)

    def test_saved_legacy_default_plan_retains_managed_installation(self):
        device, value = Device("missing"), plan(LEGACY_DEVICE_PATH)
        execution, record = self.prepare(device, value)
        self.assertEqual(record["status"], "installed")
        self.assertEqual(execution["capture"]["pmu"]["binary_path"], f"/data/local/tmp/hetero_tools/pmu/{DIGEST}/thread_pmu_sampler")
        self.assertEqual(value["capture"]["pmu"]["binary_path"], LEGACY_DEVICE_PATH)

    def test_custom_path_never_falls_back_to_installed_legacy_sampler(self):
        device = Device("missing")
        device.installed_paths.add(LEGACY_DEVICE_PATH)
        execution, record = self.prepare(device, plan("/data/local/tmp/custom"))
        self.assertEqual(record["status"], "custom_path_missing")
        self.assertEqual(len(device.calls), 1)
        self.assertFalse(execution["capture"]["pmu"]["preparation"]["allow_execution"])
        self.assertFalse(device.deployments)

    def test_missing_custom_path_and_nonexecutable_are_not_overwritten(self):
        for device, value, expected in [(Device("missing"), plan("/data/local/tmp/custom"), "custom_path_missing"),
                                        (Device("not_executable"), plan(), "not_executable")]:
            with self.subTest(expected=expected):
                execution, record = self.prepare(device, value)
                self.assertEqual(record["status"], expected)
                self.assertFalse(execution["capture"]["pmu"].pop("preparation")["allow_execution"])
                self.assertEqual(execution, value)
                self.assertFalse(device.deployments)

    def test_missing_default_installs_verified_hash_path_without_mutating_plan(self):
        device, value = Device("missing"), plan()
        original = copy.deepcopy(value)
        execution, record = self.prepare(device, value)
        self.assertEqual(record["status"], "installed")
        self.assertEqual(value, original)
        self.assertEqual(execution["capture"]["pmu"]["binary_path"], f"/data/local/tmp/hetero_tools/pmu/{DIGEST}/thread_pmu_sampler")
        self.assertEqual(record["bundle"]["source_sha256"], "b" * 64)
        self.assertEqual(len(device.deployments), 1)

    def test_repeat_capture_reuses_managed_hash_path_and_keeps_original_default(self):
        device, value = Device("missing"), plan()
        first, first_record = self.prepare(device, value)
        second, second_record = self.prepare(device, value)
        self.assertEqual(first_record["status"], "installed")
        self.assertEqual(second_record["status"], "existing")
        self.assertEqual(second_record["origin"], "managed_bundle")
        self.assertEqual(second_record["managed_file_status"], "executable")
        self.assertEqual(first["capture"]["pmu"]["binary_path"], second["capture"]["pmu"]["binary_path"])
        self.assertEqual(value["capture"]["pmu"]["binary_path"], DEFAULT_DEVICE_PATH)
        self.assertEqual(len(device.deployments), 1)

    def test_wrong_managed_identity_does_not_redeploy_or_execute(self):
        device = Device("missing", caps={**CAPS, "sampler_version": "unexpected"})
        device.installed_paths.add(f"/data/local/tmp/hetero_tools/pmu/{DIGEST}/gallery_thread_pmu")
        execution, record = self.prepare(device)
        self.assertEqual(record["status"], "incompatible")
        self.assertFalse(execution["capture"]["pmu"]["preparation"]["allow_execution"])
        self.assertFalse(device.deployments)

    def test_old_bundle_binary_name_and_installed_managed_path_are_reused(self):
        self.bundle["binary_path"] = self.root / "versions" / DIGEST / "gallery_thread_pmu_focus"
        for default in (DEFAULT_DEVICE_PATH, LEGACY_DEVICE_PATH):
            with self.subTest(default=default):
                device = Device("missing")
                old_managed = f"/data/local/tmp/hetero_tools/pmu/{DIGEST}/gallery_thread_pmu"
                device.installed_paths.add(old_managed)
                execution, record = self.prepare(device, plan(default))
                self.assertEqual(record["status"], "existing")
                self.assertEqual(record["managed_sha256_before_probe"], DIGEST)
                self.assertTrue(record["reused_legacy_managed_path"])
                self.assertEqual(execution["capture"]["pmu"]["binary_path"], old_managed)
                hash_index = next(i for i, (command, *_) in enumerate(device.calls) if command.startswith("(sha256sum "))
                probe_index = next(i for i, (command, *_) in enumerate(device.calls) if "--capabilities" in command)
                self.assertLess(hash_index, probe_index)
                self.assertFalse(device.deployments)

    def test_wrong_managed_bytes_are_rejected_before_capability_execution(self):
        device = Device("missing")
        device.installed_paths.add(f"/data/local/tmp/hetero_tools/pmu/{DIGEST}/gallery_thread_pmu")
        device.managed_hash = "b" * 64
        execution, record = self.prepare(device)
        self.assertEqual(record["code"], "PMU_INSTALLED_SAMPLER_IDENTITY_MISMATCH")
        self.assertFalse(execution["capture"]["pmu"]["preparation"]["allow_execution"])
        self.assertFalse(any("--capabilities" in command for command, *_ in device.calls))
        self.assertFalse(device.deployments)

    def test_bundle_missing_keeps_other_collectors_and_reports_configuration(self):
        device = Device("missing")
        execution, record = prepare_pmu_sampler(plan(), device, bundle_root=self.root / "missing")
        self.assertEqual(record["status"], "bundle_missing")
        self.assertEqual(execution["capture"]["collectors"], ["trace", "pmu"])
        self.assertEqual(record["remediation"]["environment"], "HETERO_PMU_SAMPLER_BUNDLE_ROOT")
        self.assertIn("provision_pmu_sampler", record["remediation"]["build_command"])
        self.assertFalse(device.deployments)

    def test_invalid_bundle_and_failed_install_never_activate_new_path(self):
        device = Device("missing")
        with patch("tools.scenario_capture.pmu_bundle.verify_bundle", side_effect=ValueError("wrong ELF")):
            execution, record = prepare_pmu_sampler(plan(), device, bundle_root=self.root)
        self.assertEqual(record["status"], "bundle_invalid")
        self.assertFalse(device.deployments)
        device.deploy = Mock(return_value={"deployment_verified": False})
        execution, record = self.prepare(device)
        self.assertEqual(record["status"], "deployment_failed")
        self.assertEqual(execution["capture"]["pmu"]["binary_path"], DEFAULT_DEVICE_PATH)

    def test_installed_wrong_version_is_not_activated(self):
        device = Device("missing", caps={**CAPS, "sampler_version": "other"})
        execution, record = self.prepare(device)
        self.assertEqual(record["code"], "PMU_INSTALLED_SAMPLER_IDENTITY_MISMATCH")
        self.assertEqual(execution["capture"]["pmu"]["binary_path"], DEFAULT_DEVICE_PATH)

    def test_probe_timeout_retains_evidence_and_existing_path(self):
        for group in ("cache", "auto", "all"):
            with self.subTest(group=group):
                device = Device()
                device.probe_timeout = True
                value = plan(); value["capture"]["pmu"]["group"] = group
                execution, record = self.prepare(device, value)
                self.assertEqual(record["status"], "probe_timed_out")
                self.assertTrue(record["probe"]["timed_out"])
                self.assertFalse(execution["capture"]["pmu"].pop("preparation")["allow_execution"])
                self.assertEqual(execution, value)
                self.assertFalse(device.deployments)

    def test_capability_shell_contract_roundtrips_and_cleans_owned_probe_directory(self):
        program = self.root / "capabilities.sh"
        program.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(json.dumps(CAPS)) + "\n")
        program.chmod(0o700)
        root = self.root

        class HostShellFixture:
            def shell(self, command, timeout=40):
                rewritten = command.replace("/data/local/tmp/hetero_pmu_probe_", str(root / "probe_"))
                result = subprocess.run(["sh", "-c", rewritten], capture_output=True, text=True, timeout=timeout)
                if result.returncode:
                    raise RuntimeError(result.stderr)
                return result.stdout.strip()

        observation = _probe(HostShellFixture(), str(program), 10)
        self.assertEqual(observation["compatibility"]["status"], "supported")
        self.assertEqual(observation["capabilities"]["sampler_version"], "1.1.0")
        self.assertFalse(observation["probe"]["timed_out"])
        self.assertEqual(list(root.glob("probe_*")), [])

    def test_cancellation_is_not_converted_to_partial(self):
        for phase in ("before", "probe", "deploy"):
            with self.subTest(phase=phase):
                device = Device("missing" if phase == "deploy" else "executable")
                if phase == "before":
                    device.cancelled = True
                elif phase == "probe":
                    device.error = CaptureCancelled("cancelled during shell")
                else:
                    device.deploy = Mock(side_effect=CaptureCancelled("cancelled during install"))
                with self.assertRaises(CaptureCancelled):
                    self.prepare(device)

    def test_unsupported_period_and_group_are_explicit(self):
        for device in (Device(compatibility="period_unsupported"), Device(caps={**CAPS, "event_groups": ["branch"]})):
            execution, record = self.prepare(device)
            self.assertEqual(record["status"], "incompatible")
            self.assertFalse(execution["capture"]["pmu"]["preparation"]["allow_execution"])
            self.assertEqual(execution["capture"]["collectors"], ["trace", "pmu"])
            self.assertFalse(device.deployments)

    def test_probe_exception_never_leaves_pmu_execution_enabled(self):
        device = Device()
        device.error = RuntimeError("transport unavailable")
        value = plan()
        value["capture"]["pmu"]["preparation"] = {"allow_execution": True}
        execution, record = self.prepare(device, value)
        self.assertEqual(record["status"], "probe_failed")
        self.assertFalse(execution["capture"]["pmu"]["preparation"]["allow_execution"])
        self.assertTrue(value["capture"]["pmu"]["preparation"]["allow_execution"])

    def test_run_and_manifest_keep_preparation_failure_while_trace_runs(self):
        manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), object())
        value = manager.create_plan({"template_id": "custom", "steps": [{"title": "操作", "operation": "wait", "seconds": 0}],
            "capture": {"selected_step_ids": ["step_01"], "collectors": ["trace", "pmu"]}})
        device = SimulatedDeviceClient()
        progress = []
        with patch("backend.pmu_sampler._availability", return_value="missing"), patch("backend.pmu_sampler.sampler_bundle_root", return_value=self.root / "missing"):
            run = manager.execute(value["plan_id"], device_serial="test_phone", connection_mode="local", device_client=device, progress=progress.append)
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["pmu_preparation"]["code"], "PMU_SAMPLER_BUNDLE_MISSING")
        self.assertEqual(run["manifest"]["pmu_preparation"], run["pmu_preparation"])
        self.assertTrue(device.executed)
        self.assertTrue(any(item.get("phase") == "pmu_preparation" for item in progress))


class DeploymentAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def result(self, remote):
        return {"status": "succeeded", "device_serial": "phone", "remote_path": remote,
            "artifact_sha256": DIGEST, "remote_sha256": DIGEST, "deployment_verified": True, "executable": True}

    def test_bridge_uses_signed_job_download_grant_and_current_lease(self):
        gateway = Mock()
        gateway.logical_device_id.return_value = "device_phone"
        gateway.create_lease.return_value = {"lease_id": "lease_old"}
        gateway.ensure_active_lease.return_value = {"effective_lease_id": "lease_new"}
        gateway._bridge_job.side_effect = lambda **kwargs: {**kwargs, "job_id": "devjob_" + "b" * 24}
        gateway.issue_download_grant.return_value = "opaque_token"
        gateway._read_registry.return_value = {"bridges": {"bridge_test": {"bridge_secret": "test_secret"}}}
        gateway._sign_job.return_value = "signed"
        remote = "/data/local/tmp/hetero_tools/pmu/sampler"
        gateway.dispatch_bridge_job.return_value = {"result": self.result(remote)}
        client = GatewayDeviceClient(gateway, state_root=self.root, project_id="test", bridge_id="bridge_test",
            device_serial="phone", run_id="capture_" + "c" * 24, run_dir=self.root)
        result = client.deploy(self.root / "sampler", remote, DIGEST)
        job = gateway.dispatch_bridge_job.call_args.args[0]
        self.assertEqual(job["lease_id"], "lease_new")
        self.assertEqual(job["signature"], "signed")
        self.assertIn("/artifact?token=opaque_token", job["payload"]["download_url"])
        self.assertNotIn("local_file", job["payload"])
        gateway.local_deploy.assert_not_called()
        self.assertEqual(result["executor"], "registered_windows_bridge")
        gateway.dispatch_bridge_job.return_value = {"result": {"status": "cancelled"}}
        with self.assertRaises(CaptureCancelled):
            client.deploy(self.root / "sampler", remote, DIGEST)

    def test_local_deployment_tracks_capture_owner_and_rejects_hash_mismatch(self):
        gateway = Mock()
        remote = "/data/local/tmp/managed_sampler"
        gateway.local_deploy.return_value = self.result(remote)
        client = LocalDeviceClient(gateway, device_serial="phone", run_id="capture_test", run_dir=self.root)
        result = client.deploy(self.root / "sampler", remote, DIGEST)
        self.assertTrue(result["deployment_verified"])
        self.assertEqual(gateway.local_deploy.call_args.kwargs["agent_run_id"], "capture_test:capture")
        gateway.local_deploy.return_value = {**self.result(remote), "remote_sha256": "b" * 64}
        with self.assertRaisesRegex(RuntimeError, "could not be verified"):
            client.deploy(self.root / "sampler", remote, DIGEST)


if __name__ == "__main__":
    unittest.main()
