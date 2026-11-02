from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import AsyncMock, Mock, patch

from backend.device_gateway import DEVICE_ACTIONS


BRIDGE_PATH = Path(__file__).resolve().parents[2] / "device-bridge" / "hdc_device_bridge.py"
RUN_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "deploy" / "windows" / "run-device-bridge.ps1"
SPEC = importlib.util.spec_from_file_location("heterogeneous_device_bridge_contract", BRIDGE_PATH)
assert SPEC is not None and SPEC.loader is not None
BRIDGE_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE_MODULE)
HdcDeviceBridge = BRIDGE_MODULE.HdcDeviceBridge


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class _FakeWebSocketConnection(_FakeWebSocket):
    def __init__(self, bridge_id: str) -> None:
        super().__init__()
        self.bridge_id = bridge_id

    async def __aenter__(self) -> "_FakeWebSocketConnection":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def recv(self) -> str:
        return json.dumps({
            "type": "bridge.welcome",
            "protocol_version": BRIDGE_MODULE.WEBSOCKET_PROTOCOL_VERSION,
            "bridge_id": self.bridge_id,
        })


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode = 0
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"hdc output", b""

    async def wait(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _BlockingProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._released = asyncio.Event()
        self.communicate_started = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_started.set()
        await self._released.wait()
        return b"", b""

    async def wait(self) -> int:
        await self._released.wait()
        return int(self.returncode or 0)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._released.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._released.set()


class DeviceBridgeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.hdc = root / "hdc.exe"
        self.hdc.write_bytes(b"placeholder")
        self.hdc.chmod(0o755)
        self.bridge = HdcDeviceBridge(
            {
                "bridge_id": "bridge_aaaaaaaaaaaaaaaaaaaaaaaa",
                "bridge_secret": "bridge-test-secret",
                "platform_http_url": "https://platform.example",
                "platform_ws_url": "wss://platform.example/v1/device-bridges/ws",
                "hdc_path": str(self.hdc),
                "spool_root": str(root / "spool"),
            },
            root / "bridge.json",
        )

    def _signed_shell_job(self) -> dict[str, object]:
        job: dict[str, object] = {
            "schema_version": "device_job/v1",
            "job_id": "devjob_aaaaaaaaaaaaaaaaaaaaaaaa",
            "request_id": "req_aaaaaaaaaaaaaaaaaaaaaaaa",
            "idempotency_key": "bridge-contract-shell-v1",
            "project_id": "local-default",
            "agent_run_id": "run_aaaaaaaaaaaaaaaaaaaaaaaa",
            "bridge_id": self.bridge.bridge_id,
            "device_lease_id": "lease_aaaaaaaaaaaaaaaaaaaaaaaa",
            "logical_device_id": "device_contract_phone",
            "action": "device.execute_shell",
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "payload": {"device_serial": "contract-phone", "command": "echo verified"},
        }
        job["signature"] = hmac.new(
            self.bridge.bridge_secret.encode("utf-8"),
            BRIDGE_MODULE._canonical_json(job),
            hashlib.sha256,
        ).hexdigest()
        return job

    def test_windows_runner_is_a_single_command_bootstrap_and_daemon(self) -> None:
        content = RUN_SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn('Get-Command $CommandName', content)
        self.assertIn('[guid]::NewGuid()', content)
        self.assertIn('requirements.sha256', content)
        self.assertIn('"pair"', content)
        self.assertIn('"doctor"', content)
        self.assertIn('run --config $Config', content)
        self.assertIn('HETERO_PLATFORM_URL', content)

    def test_runtime_refreshes_device_discovery_while_connected(self) -> None:
        content = BRIDGE_PATH.read_text(encoding="utf-8")

        self.assertIn("DIAGNOSTICS_REFRESH_SECONDS = 30", content)
        refresh_start = content.index("async def refresh_diagnostics()")
        refresh_end = content.index("heartbeat_task =", refresh_start)
        refresh = content[refresh_start:refresh_end]
        self.assertIn("await self._refresh_runtime_diagnostics(force=True)", refresh)
        self.assertIn("diagnostics_task = asyncio.create_task(refresh_diagnostics())", content)

    def test_websocket_doctor_verifies_the_bridge_protocol_without_compression(self) -> None:
        connection = _FakeWebSocketConnection(self.bridge.bridge_id)
        with patch.object(BRIDGE_MODULE.websockets, "connect", return_value=connection) as connect:
            result = asyncio.run(BRIDGE_MODULE._platform_websocket_health(self.bridge))

        self.assertTrue(result["reachable"])
        self.assertTrue(result["protocol_verified"])
        self.assertEqual(connection.sent[0]["type"], "bridge.hello")
        self.assertEqual(connection.sent[0]["bridge_id"], self.bridge.bridge_id)
        self.assertEqual(connection.sent[0]["bridge_secret"], self.bridge.bridge_secret)
        self.assertIsNone(connect.call_args.kwargs["compression"])

    def test_default_bridge_transport_is_resilient_http_polling(self) -> None:
        self.assertEqual(self.bridge.transport, "http_poll")
        self.assertEqual(self.bridge.capabilities()["protocol_version"], BRIDGE_MODULE.HTTP_POLL_PROTOCOL_VERSION)
        self.assertEqual(self.bridge.capabilities()["transport"], "http_poll")

    def test_device_probe_batches_properties_and_runtime_paths_into_one_hdc_call(self) -> None:
        websocket = _FakeWebSocket()
        output = {
            "return_code": 0,
            "timed_out": False,
            "cancelled": False,
            "stdout": "\n".join([
                "__HETERO_PROPERTY__model=Mate Test",
                "__HETERO_PROPERTY__hardware=kirin-test",
                "__HETERO_PROPERTY__chipset=chip-test",
                "__HETERO_PROPERTY__os_full_name=OpenHarmony Test",
                "__HETERO_PROPERTY__api_version=20",
                "__HETERO_PROPERTY__gpu_vendor=Vendor Test",
                "__HETERO_RUNTIME_PATH__gpu=/system/lib64/libvulkan.so",
                "__HETERO_RUNTIME_PATH__npu=/system/lib64/ndk/libneural_network_runtime.so",
            ]),
            "stderr": "",
            "duration_ms": 42.0,
            "invocation": {"executable_name": "hdc.exe", "arguments": []},
        }
        job = {
            "job_id": "devjob_" + "a" * 24,
            "request_id": "req_probe_batch",
            "action": "device.probe",
            "payload": {"device_serial": "device-test-01"},
        }

        with patch.object(self.bridge, "_run_hdc", AsyncMock(return_value=output)) as run_hdc:
            result = asyncio.run(self.bridge._probe(websocket, job))

        run_hdc.assert_awaited_once()
        arguments = run_hdc.await_args.args
        self.assertEqual(arguments[:3], ("-t", "device-test-01", "shell"))
        self.assertIn("param get const.product.model", arguments[3])
        self.assertIn("/system/lib64/libvulkan.so", arguments[3])
        self.assertEqual(run_hdc.await_args.kwargs["timeout_seconds"], 30)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["device"]["model"], "Mate Test")
        self.assertEqual(result["observed_units"]["gpu"]["observation_status"], "observed")
        self.assertEqual(result["observed_units"]["dsp"]["observation_status"], "not_observed")
        self.assertEqual(result["probe_profile"], "bridge-hdc-readonly-capability-probe/v2")
        self.assertEqual(
            [message["progress"]["stage"] for message in websocket.sent],
            ["device_probe_batch_started", "device_probe_batch_completed"],
        )

    def test_cancelling_bridge_runtime_terminates_the_active_hdc_child(self) -> None:
        process = _BlockingProcess()

        async def exercise() -> None:
            with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                task = asyncio.create_task(
                    self.bridge._run_hdc("list", "targets", timeout_seconds=30, job_id="devjob_" + "b" * 24)
                )
                await process.communicate_started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(exercise())
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)

    def test_only_one_daemon_can_own_a_bridge_identity_and_spool(self) -> None:
        first = BRIDGE_MODULE._acquire_run_lock(self.bridge.config_path)
        try:
            with self.assertRaisesRegex(RuntimeError, "another Device Bridge process"):
                BRIDGE_MODULE._acquire_run_lock(self.bridge.config_path)
        finally:
            BRIDGE_MODULE._release_run_lock(first)

        second = BRIDGE_MODULE._acquire_run_lock(self.bridge.config_path)
        BRIDGE_MODULE._release_run_lock(second)

    def test_http_error_preserves_endpoint_and_platform_error_body(self) -> None:
        body = json.dumps({
            "error": {
                "code": "BRIDGE_JOB_RESULT_STALE",
                "message": "job no longer exists",
                "details": {"job_id": "devjob_old"},
            }
        }).encode("utf-8")
        error = HTTPError(
            "https://platform.example/v1/device-bridges/events",
            410,
            "Gone",
            {},
            BytesIO(body),
        )
        with (
            patch.object(BRIDGE_MODULE, "_direct_urlopen", side_effect=error),
            self.assertRaises(BRIDGE_MODULE.PlatformHttpError) as raised,
        ):
            BRIDGE_MODULE._http_json(
                "https://platform.example/v1/device-bridges/events",
                {"message": {"type": "job.result"}},
            )

        self.assertEqual(raised.exception.status_code, 410)
        self.assertEqual(raised.exception.error_code, "BRIDGE_JOB_RESULT_STALE")
        self.assertIn("/v1/device-bridges/events", str(raised.exception))

    def test_platform_http_ignores_ambient_host_proxies(self) -> None:
        opener = Mock()
        response = object()
        opener.open.return_value = response

        with patch.object(BRIDGE_MODULE, "build_opener", return_value=opener) as build:
            observed = BRIDGE_MODULE._direct_urlopen(
                BRIDGE_MODULE.Request("http://192.168.10.20:4032/v1/live"),
                timeout=20,
            )

        proxy_handler = build.call_args.args[0]
        self.assertEqual(proxy_handler.proxies, {})
        opener.open.assert_called_once()
        self.assertIs(observed, response)

    def test_stale_spooled_result_is_quarantined_without_blocking_transport(self) -> None:
        job_id = "devjob_" + "e" * 24
        envelope = {
            "type": "job.result",
            "job_id": job_id,
            "bridge_id": self.bridge.bridge_id,
            "result": {"status": "succeeded"},
        }
        self.bridge._persist_job(job_id, {
            "job_id": job_id,
            "state": "succeeded",
            "result_envelope": envelope,
        })
        self.bridge._queue_completed_results()
        stale = BRIDGE_MODULE.PlatformHttpError(
            url="https://platform.example/v1/device-bridges/events",
            status_code=410,
            error_code="BRIDGE_JOB_RESULT_STALE",
            message="job no longer exists",
            response_preview="",
        )

        with patch.object(BRIDGE_MODULE, "_http_json", side_effect=stale):
            asyncio.run(self.bridge._flush_http_events("connection_" + "f" * 32))

        self.assertEqual(len(self.bridge._http_events), 0)
        self.assertEqual(self.bridge.jobs[job_id]["result_delivery"]["state"], "quarantined")
        self.assertIn("BRIDGE_JOB_RESULT_STALE", (self.bridge.spool_root / "quarantined-events.jsonl").read_text(encoding="utf-8"))

    def test_idempotently_ignored_stale_result_is_quarantined_after_ack(self) -> None:
        job_id = "devjob_" + "d" * 24
        envelope = {
            "type": "job.result",
            "job_id": job_id,
            "bridge_id": self.bridge.bridge_id,
            "result": {"status": "succeeded"},
        }
        self.bridge._persist_job(job_id, {
            "job_id": job_id,
            "state": "succeeded",
            "result_envelope": envelope,
        })
        self.bridge._queue_completed_results()

        with patch.object(BRIDGE_MODULE, "_http_json", return_value={
            "acknowledged": True,
            "event_disposition": "stale_ignored",
            "reason_code": "BRIDGE_JOB_RESULT_STALE",
        }):
            asyncio.run(self.bridge._flush_http_events("connection_" + "c" * 32))

        self.assertEqual(len(self.bridge._http_events), 0)
        self.assertEqual(self.bridge.jobs[job_id]["result_delivery"]["state"], "quarantined")
        self.assertEqual(self.bridge.jobs[job_id]["result_delivery"]["reason_code"], "BRIDGE_JOB_RESULT_STALE")

    def test_spool_from_an_old_bridge_identity_is_quarantined_locally(self) -> None:
        job_id = "devjob_" + "f" * 24
        self.bridge._persist_job(job_id, {
            "job_id": job_id,
            "state": "succeeded",
            "result_envelope": {
                "type": "job.result",
                "job_id": job_id,
                "bridge_id": "bridge_" + "1" * 24,
                "result": {"status": "succeeded"},
            },
        })

        self.bridge._queue_completed_results()

        self.assertEqual(len(self.bridge._http_events), 0)
        self.assertEqual(self.bridge.jobs[job_id]["result_delivery"]["reason_code"], "BRIDGE_SPOOL_IDENTITY_MISMATCH")

    def test_capability_catalog_matches_gateway_contract(self) -> None:
        advertised = set(self.bridge.capabilities()["actions"])

        self.assertEqual(advertised, set(DEVICE_ACTIONS))
        self.assertNotIn("shell", advertised)
        self.assertEqual(self.bridge.capabilities()["deployment_contract_version"], "device-deployment/v2")

    def test_runtime_diagnostics_report_the_configured_hdc_targets_and_spool(self) -> None:
        command_results = [
            {"return_code": 0, "stdout": "Ver: 3.1.0\n", "stderr": "", "timed_out": False},
            {"return_code": 0, "stdout": "contract-phone\n", "stderr": "", "timed_out": False},
        ]
        with patch.object(self.bridge, "_run_hdc", new=AsyncMock(side_effect=command_results)):
            diagnostics = asyncio.run(self.bridge._refresh_runtime_diagnostics(force=True))

        self.assertEqual(diagnostics["status"], "ready")
        self.assertEqual(diagnostics["hdc"]["target_count"], 1)
        self.assertTrue(diagnostics["hdc"]["configured_path_exists"])
        self.assertTrue(diagnostics["spool"]["writable"])
        self.assertEqual(self.bridge.capabilities()["runtime_diagnostics"], diagnostics)

    def test_discovery_returns_fresh_bridge_diagnostics_to_the_agent(self) -> None:
        command_results = [
            {"return_code": 0, "stdout": "contract-phone\n", "stderr": "", "timed_out": False},
            {"return_code": 0, "stdout": "Ver: 3.1.0\n", "stderr": "", "timed_out": False},
        ]
        with patch.object(self.bridge, "_run_hdc", new=AsyncMock(side_effect=command_results)):
            result = asyncio.run(self.bridge._discover({"job_id": "devjob_discovery"}))

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["devices"][0]["device_serial"], "contract-phone")
        self.assertEqual(result["bridge_diagnostics"]["status"], "ready")

    def test_hdc_process_start_failure_is_returned_as_structured_diagnostic(self) -> None:
        with patch.object(
            BRIDGE_MODULE.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(side_effect=PermissionError("blocked by policy")),
        ):
            result = asyncio.run(self.bridge._run_hdc("list", "targets", timeout_seconds=10))

        self.assertIsNone(result["return_code"])
        self.assertEqual(result["launch_error"]["code"], "BRIDGE_HDC_PROCESS_START_FAILED")
        self.assertEqual(result["invocation"]["arguments"], ["list", "targets"])

    def test_doctor_transfer_check_verifies_and_cleans_a_temporary_device_file(self) -> None:
        payload = b"d" * 96
        digest = hashlib.sha256(payload).hexdigest()
        command_results = [
            {"return_code": 0, "stdout": "Ver: 3.1.0\n", "stderr": "", "timed_out": False},
            {"return_code": 0, "stdout": "contract-phone\n", "stderr": "", "timed_out": False},
            {"return_code": 0, "stdout": "", "stderr": "", "timed_out": False},
            {"return_code": 0, "stdout": "send ok", "stderr": "", "timed_out": False},
            {"return_code": 0, "stdout": f"{digest}  bridge-doctor.bin\n", "stderr": "", "timed_out": False},
            {"return_code": 0, "stdout": "", "stderr": "", "timed_out": False},
        ]
        with (
            patch.object(self.bridge, "_run_hdc", new=AsyncMock(side_effect=command_results)),
            patch.object(BRIDGE_MODULE.os, "urandom", return_value=payload),
            patch.object(BRIDGE_MODULE, "_platform_health", return_value={"reachable": True, "status_code": 200}),
            patch.object(BRIDGE_MODULE, "_platform_http_poll_health", new=AsyncMock(return_value={"reachable": True, "protocol_verified": True})),
        ):
            report = asyncio.run(BRIDGE_MODULE._doctor_report(self.bridge, transfer_check=True, requested_serial=""))

        self.assertEqual(report["status"], "ready")
        self.assertTrue(report["transfer_check"]["verified"])
        self.assertEqual(report["transfer_check"]["device_serial"], "contract-phone")
        self.assertEqual(report["transfer_check"]["cleanup"]["return_code"], 0)

    def test_device_remote_parent_always_uses_posix_separators(self) -> None:
        self.assertEqual(
            BRIDGE_MODULE._remote_parent("/data/local/tmp/hetero_runs/contract/experiment.bin"),
            "/data/local/tmp/hetero_runs/contract",
        )

    def test_trusted_lan_http_and_ws_are_valid_without_pairing_transport_gate(self) -> None:
        self.assertEqual(
            BRIDGE_MODULE._validated_platform_url("http://192.168.10.20:4032"),
            "http://192.168.10.20:4032",
        )
        bridge = HdcDeviceBridge(
            {
                "bridge_id": "bridge_bbbbbbbbbbbbbbbbbbbbbbbb",
                "bridge_secret": "bridge-test-secret",
                "platform_http_url": "http://192.168.10.20:4032",
                "platform_ws_url": "ws://192.168.10.20:4032/v1/device-bridges/ws",
                "hdc_path": str(self.hdc),
                "spool_root": str(Path(self.tempdir.name) / "trusted-lan-spool"),
            },
            Path(self.tempdir.name) / "trusted-lan-bridge.json",
        )

        self.assertEqual(bridge.platform_ws_url, "ws://192.168.10.20:4032/v1/device-bridges/ws")

    def test_pair_command_registers_without_a_pairing_code(self) -> None:
        config_path = Path(self.tempdir.name) / "zero-touch-bridge.json"
        args = BRIDGE_MODULE.argparse.Namespace(
            platform_url="http://192.168.10.20:4032",
            pairing_code="",
            hdc=str(self.hdc),
            name="zero-touch-windows",
            config=str(config_path),
        )
        registration_response = {
            "bridge_id": "bridge_cccccccccccccccccccccccc",
            "bridge_secret": "zero-touch-secret",
            "registration_mode": "trusted_single_user",
        }

        with (
            patch.object(BRIDGE_MODULE, "_http_json", return_value=registration_response) as register,
            patch("builtins.print"),
        ):
            BRIDGE_MODULE.pair(args)

        request_url, payload = register.call_args.args
        self.assertEqual(request_url, "http://192.168.10.20:4032/v1/device-bridges/register")
        self.assertNotIn("pairing_code", payload)
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["registration_mode"], "trusted_single_user")
        self.assertEqual(saved["transport"], "http_poll")
        self.assertEqual(saved["platform_ws_url"], "ws://192.168.10.20:4032/v1/device-bridges/ws")

    def test_fixed_hdc_argv_does_not_create_a_host_shell(self) -> None:
        fake_process = _FakeProcess()
        with patch.object(BRIDGE_MODULE.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=fake_process)) as create:
            result = asyncio.run(
                self.bridge._run_hdc(
                    "-t",
                    "contract-phone",
                    "shell",
                    "echo $HOME; touch /data/local/tmp/bridge-contract",
                    timeout_seconds=10,
                    job_id="devjob_aaaaaaaaaaaaaaaaaaaaaaaa",
                )
            )

        self.assertEqual(result["return_code"], 0)
        args, kwargs = create.call_args
        self.assertEqual(args[0], str(self.hdc.resolve()))
        self.assertEqual(args[1:4], ("-t", "contract-phone", "shell"))
        self.assertEqual(args[4], "echo $HOME; touch /data/local/tmp/bridge-contract")
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stdin"], BRIDGE_MODULE.asyncio.subprocess.DEVNULL)

    def test_finished_job_is_spooled_and_replayed_without_second_device_execution(self) -> None:
        job = self._signed_shell_job()
        websocket = _FakeWebSocket()
        calls = 0

        async def execute_shell(_: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"status": "succeeded", "device_serial": "contract-phone", "execution": {"return_code": 0}}

        self.bridge._execute_shell = execute_shell
        asyncio.run(self.bridge._execute_job(websocket, job))
        asyncio.run(self.bridge._execute_job(websocket, job))

        self.assertEqual(calls, 1)
        result_frames = [item for item in websocket.sent if item.get("type") == "job.result"]
        self.assertEqual(len(result_frames), 2)
        self.assertEqual(result_frames[0]["result"], result_frames[1]["result"])

    def test_remote_deploy_reports_linux_to_windows_and_hdc_transfer_stages(self) -> None:
        source = Path(self.tempdir.name) / "spool" / "artifact.bin"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"device-binary")
        artifact_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        transfer = {
            "transport": "platform_http_to_windows_spool",
            "bytes": source.stat().st_size,
            "sha256": artifact_sha,
            "duration_ms": 12.5,
            "chunk_bytes": 1024 * 1024,
            "verified": True,
        }
        job = {
            "job_id": "devjob_bbbbbbbbbbbbbbbbbbbbbbbb",
            "request_id": "req_bbbbbbbbbbbbbbbbbbbbbbbb",
            "action": "device.deploy_artifact",
            "payload": {
                "device_serial": "contract-phone",
                "remote_path": "/data/local/tmp/hetero_runs/contract/experiment.bin",
                "artifact_sha256": artifact_sha,
                "download_url": "/v1/device-bridges/jobs/devjob_bbbbbbbbbbbbbbbbbbbbbbbb/artifact?token=opaque",
            },
        }
        websocket = _FakeWebSocket()
        hdc_results = [
            {"return_code": 0, "stdout": "", "stderr": ""},
            {"return_code": 0, "stdout": "send ok", "stderr": ""},
            {"return_code": 0, "stdout": "", "stderr": ""},
            {"return_code": 0, "stdout": f"{artifact_sha}  experiment.bin\n", "stderr": ""},
        ]

        with (
            patch.object(self.bridge, "_download_artifact", new=AsyncMock(return_value=(source, transfer))),
            patch.object(self.bridge, "_run_hdc", new=AsyncMock(side_effect=hdc_results)) as run_hdc,
        ):
            result = asyncio.run(self.bridge._deploy(websocket, job))

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["artifact_transfer"], transfer)
        stages = [item["progress"]["stage"] for item in websocket.sent if item.get("type") == "job.progress"]
        self.assertEqual(
            stages,
            [
                "artifact_download_started",
                "artifact_download_verified",
                "hdc_file_send_started",
                "hdc_file_send_completed",
                "device_deployment_verified",
            ],
        )
        mkdir_args = run_hdc.call_args_list[0].args
        self.assertEqual(mkdir_args[3], "mkdir -p /data/local/tmp/hetero_runs/contract")
        file_send_args = run_hdc.call_args_list[1].args
        self.assertEqual(file_send_args[:5], ("-t", "contract-phone", "file", "send", str(source)))
        self.assertEqual(file_send_args[5], "/data/local/tmp/hetero_runs/contract/experiment.bin")
        self.assertTrue(result["deployment_verified"])
        self.assertTrue(result["device_transfer"]["verified"])

    def test_remote_deploy_reports_artifact_download_failure_stage(self) -> None:
        job = {
            "job_id": "devjob_cccccccccccccccccccccccc",
            "request_id": "req_cccccccccccccccccccccccc",
            "action": "device.deploy_artifact",
            "payload": {
                "device_serial": "contract-phone",
                "remote_path": "/data/local/tmp/hetero_runs/contract/experiment.bin",
                "artifact_sha256": "c" * 64,
                "download_url": "/v1/device-bridges/jobs/devjob_cccccccccccccccccccccccc/artifact?token=opaque",
            },
        }
        websocket = _FakeWebSocket()

        with patch.object(self.bridge, "_download_artifact", new=AsyncMock(side_effect=RuntimeError("download reset"))):
            result = asyncio.run(self.bridge._deploy(websocket, job))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "artifact_download")
        self.assertEqual(result["failure_domain"], "linux_to_windows_transport")
        self.assertEqual(result["error"]["code"], "BRIDGE_ARTIFACT_DOWNLOAD_FAILED")


if __name__ == "__main__":
    unittest.main()
