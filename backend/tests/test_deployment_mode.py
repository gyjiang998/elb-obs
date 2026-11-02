from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend import app as platform_app
from backend.core import PlatformError
from backend.deployment_mode import (
    deployment_capabilities,
    request_needs_build_execution,
    request_needs_device_validation,
    require_action_capability,
)
from backend.runtime_action_router import PlatformRuntimeActionRouter


class DeploymentModeTests(unittest.TestCase):
    def test_existing_deployments_keep_device_validation(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(deployment_capabilities()["build_execution_enabled"])
            self.assertTrue(deployment_capabilities()["device_validation_enabled"])

    def test_analysis_deployment_blocks_http_execution_before_side_effects(self) -> None:
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "analysis"}), \
                patch.object(platform_app, "build_worker") as worker, \
                patch.object(platform_app, "device_gateway") as gateway:
            client = TestClient(platform_app.app)
            for method, path, error_code in (
                ("GET", "/v1/device-verification/devices", "DEPLOYMENT_DEVICE_VALIDATION_DISABLED"),
                ("GET", "/v1/device-experiment-profiles", "DEPLOYMENT_BUILD_EXECUTION_DISABLED"),
                ("POST", "/v1/builds", "DEPLOYMENT_BUILD_EXECUTION_DISABLED"),
                ("POST", "/v1/runtime-assets/openharmony-sdk/provision", "DEPLOYMENT_BUILD_EXECUTION_DISABLED"),
                ("POST", "/v1/device-leases", "DEPLOYMENT_DEVICE_VALIDATION_DISABLED"),
                ("POST", "/v1/device-bridges/register", "DEPLOYMENT_DEVICE_VALIDATION_DISABLED"),
                ("POST", "/v1/projects/local-default/device-shell-runs", "DEPLOYMENT_DEVICE_VALIDATION_DISABLED"),
                ("POST", "/v1/projects/local-default/device-validation-runs", "DEPLOYMENT_DEVICE_VALIDATION_DISABLED"),
            ):
                with self.subTest(path=path):
                    response = client.request(method, path, json={} if method == "POST" else None)
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(response.json()["error"]["code"], error_code)
            self.assertEqual(worker.mock_calls, [])
            self.assertEqual(gateway.mock_calls, [])
            self.assertEqual(client.get("/v1/live").status_code, 200)
            self.assertFalse(client.get("/v1/deployment").json()["build_execution_enabled"])
            self.assertFalse(client.get("/v1/deployment").json()["device_validation_enabled"])

    def test_worker_enables_builds_and_rejects_every_server_device_surface(self) -> None:
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "worker"}), \
                patch.object(platform_app, "experiment_profile_catalog", return_value=[]) as profiles, \
                patch.object(platform_app, "device_gateway") as gateway:
            client = TestClient(platform_app.app)
            deployment = client.get("/v1/deployment").json()
            self.assertTrue(deployment["build_execution_enabled"])
            self.assertFalse(deployment["device_validation_enabled"])
            self.assertEqual(client.get("/v1/device-experiment-profiles").status_code, 200)
            profiles.assert_called_once()
            self.assertEqual(client.post("/v1/builds", json={}).status_code, 422, "build endpoint must pass the worker capability gate")
            self.assertEqual(client.post("/v1/runtime-assets/openharmony-sdk/provision", json={}).status_code, 422)
            for method, path in (
                ("GET", "/v1/device-verification/devices"),
                ("GET", "/v1/device-bridges"),
                ("POST", "/v1/device-bridges/poll"),
                ("POST", "/v1/device-leases"),
                ("POST", "/v1/projects/local-default/device-shell-runs"),
                ("POST", "/v1/scenario-capture/devices/discover"),
                ("POST", "/v1/scenario-capture/plans/plan_123/execute"),
            ):
                with self.subTest(path=path):
                    response = client.request(method, path, json={} if method == "POST" else None)
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(response.json()["error"]["code"], "DEPLOYMENT_DEVICE_VALIDATION_DISABLED")
            self.assertEqual(gateway.mock_calls, [])
            with self.assertRaises(WebSocketDisconnect) as raised:
                with client.websocket_connect("/v1/device-bridges/ws"):
                    pass
            self.assertEqual(raised.exception.code, 4403)

        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "worker"}):
            require_action_capability("build.describe_profile")
            for action in ("device.discover", "scenario_capture.execute", "scenario_capture.discover_devices"):
                with self.subTest(action=action), self.assertRaises(PlatformError) as raised:
                    require_action_capability(action)
                self.assertEqual(raised.exception.code, "DEPLOYMENT_DEVICE_VALIDATION_DISABLED")

    def test_analysis_deployment_blocks_mcp_and_direct_router_execution(self) -> None:
        router = object.__new__(PlatformRuntimeActionRouter)
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "analysis"}):
            for action in (
                "device.discover", "device.deploy_artifact", "device.execute_shell",
                "scenario_capture.execute", "scenario_capture.discover_devices",
                "build.describe_profile", "build.create",
            ):
                for method in (router.validate, router.execute):
                    with self.subTest(action=action, method=method.__name__), self.assertRaises(PlatformError) as raised:
                        method(action, {}, {})
                    expected = "DEPLOYMENT_BUILD_EXECUTION_DISABLED" if action.startswith("build.") else "DEPLOYMENT_DEVICE_VALIDATION_DISABLED"
                    self.assertEqual(raised.exception.code, expected)

    def test_history_trace_analysis_and_unrelated_builds_remain_accessible(self) -> None:
        for method, path in (
            ("GET", "/v1/builds/art_saved"),
            ("GET", "/v1/projects/local-default/device-validation-runs/art_saved"),
            ("POST", "/v1/application-flows/runtime-traces"),
            ("POST", "/v1/code-graphs/builds"),
        ):
            with self.subTest(path=path):
                self.assertFalse(request_needs_device_validation(method, path))

    def test_build_and_device_route_classification_are_independent(self) -> None:
        for method, path in (
            ("POST", "/v1/builds"),
            ("GET", "/v1/device-experiment-profiles"),
            ("POST", "/v1/runtime-assets/openharmony-sdk/provision"),
        ):
            with self.subTest(path=path):
                self.assertTrue(request_needs_build_execution(method, path))
                self.assertFalse(request_needs_device_validation(method, path))
        for method, path in (
            ("GET", "/v1/device-bridges"),
            ("POST", "/v1/device-bridges/events"),
            ("POST", "/v1/scenario-capture/devices/discover"),
            ("POST", "/v1/scenario-capture/plans/plan_123/execute"),
        ):
            with self.subTest(path=path):
                self.assertFalse(request_needs_build_execution(method, path))
                self.assertTrue(request_needs_device_validation(method, path))

    def test_device_mode_http_discovery_reaches_existing_implementation(self) -> None:
        with patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "device"}), \
                patch.object(platform_app, "discover_devices", return_value={"devices": [], "hdc": {"available": True}}) as discover:
            response = TestClient(platform_app.app).get("/v1/device-verification/devices")
        self.assertEqual(response.status_code, 200, response.text)
        discover.assert_called_once()
