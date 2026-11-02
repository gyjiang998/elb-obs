from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from backend.agent_runtime import AgentRunStore, AgentRuntime, CredentialLeaseStore
from backend.core import ArtifactStore
from backend.parallel_workbench import ParallelWorkbench, TERMINAL
from backend.runtime_action_router import PlatformRuntimeActionRouter
from backend.tests.test_parallel_workbench import StubGateway


class ParallelActionsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        store = ArtifactStore(root)
        gateway = StubGateway()
        self.service = ParallelWorkbench(root, store, gateway)
        self.addCleanup(self.service.shutdown)
        self.connector = Mock(side_effect=AssertionError("Capture/modeling must not require a source connector"))
        self.router = PlatformRuntimeActionRouter(artifact_store=store, build_worker=object(), device_gateway=gateway,
            default_source_connector=self.connector, bridge_artifact_url=lambda *_: "unused", parallel_workbench=self.service)
        self.runtime = AgentRuntime(store=AgentRunStore(root / "agent-runs"), leases=CredentialLeaseStore(),
            model_gateway=Mock(side_effect=AssertionError("No model network request is permitted")),
            action_router=self.router, system_prompt=lambda _: "test")
        self.run = self.runtime.create(project_id="local-default", goal="Scenario and modeling without source", initial_input="test",
            mode="page_agent", context={}, budget={}, action_catalog_version="test", skill_versions=[])
        self.environment = patch.dict(os.environ, {"HETERO_DEPLOYMENT_MODE": "device", "HETERO_AGENT_RUN_ID": self.run["run_id"],
                                                   "HETERO_AGENT_TOOL_TOKEN": "parallel-actions-test-token"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.mcp = importlib.import_module("backend.kimi_mcp_server")

    def action(self, name, value=None):
        observation = self.runtime.execute_external_action(self.run["run_id"], name, value or {})
        self.assertEqual(observation["status"], "succeeded", observation)
        self.connector.assert_not_called()
        return observation

    def wait_job(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.service.get_job(job_id)
            if job["status"] in TERMINAL:
                self.assertEqual(job["status"], "succeeded", job)
                return job
            time.sleep(.005)
        self.fail("test job did not finish")

    def mcp_request(self, method, path, body=None, **kwargs):
        self.assertEqual(method, "POST")
        prefix = f'/v1/internal/agent-runs/{self.run["run_id"]}/actions/'
        self.assertTrue(path.startswith(prefix), path)
        return self.runtime.execute_external_action(self.run["run_id"], path[len(prefix):], body["input"])

    def test_router_plans_and_script_work_without_source_connector(self):
        self.assertEqual(self.router.owner("scenario_capture.create_plan"), "platform")
        catalog = self.action("scenario_capture.catalog")["result"]
        self.assertEqual(catalog["default_template_id"], "gallery_17_steps_v1")
        plan = self.action("scenario_capture.create_plan", {"capture": {"selected_step_ids": ["step_07"]}})["result"]
        self.assertEqual(plan["source_connector_id"], "unbound")
        self.assertEqual(self.action("scenario_capture.read_plan", {"plan_id": plan["plan_id"]})["result"]["plan_id"], plan["plan_id"])
        first = self.action("scenario_capture.generate_script", {"plan_id": plan["plan_id"]})
        second = self.action("scenario_capture.generate_script", {"plan_id": plan["plan_id"]})
        self.assertIn("PLAN =", first["result"]["content"])
        self.assertTrue(second["reused_from_checkpoint"])

    def test_workload_feature_mcp_chain_retains_profile_in_analysis(self):
        dataset = self.action("parallel_modeling.import_dataset", {"builtin_id": "gallery-edit-pmu-20261014"})["result"]
        with patch.object(self.mcp, "RUN_ID", self.run["run_id"]), patch.object(self.mcp, "_request", side_effect=self.mcp_request):
            catalog = self.mcp.workload_features_catalog()
            self.assertEqual(catalog["status"], "succeeded", catalog)
            profile = self.mcp.workload_features_analyze(dataset["id"])["result"]
            self.assertEqual(len(profile["stages"]), 26)
            loaded = self.mcp.workload_features_read_profile(profile["id"])["result"]
            self.assertEqual(loaded["profile_sha256"], profile["profile_sha256"])
            job = self.mcp.parallel_modeling_analyze(dataset["id"], feature_profile_id=profile["id"])["result"]
            result = self.wait_job(job["job_id"])["result"]
            self.assertEqual(result["feature_profile_id"], profile["id"])
            self.assertEqual(result["workload_profile"]["dataset_id"], dataset["id"])
        self.assertFalse(self.router.can_reuse("workload_features.list_profiles", {}, {}, self.run))
        self.connector.assert_not_called()

    def test_agent_polling_refreshes_after_background_job_changes(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def operation(**kwargs):
            entered.set()
            release.wait(3)
            return {"status": "completed", "actual_result": 42}

        job = self.service.submit("test", operation, inputs={})
        self.assertTrue(entered.wait(2))
        first = self.action("parallel_job.read", {"job_id": job["job_id"]})
        self.assertEqual(first["result"]["status"], "running")
        release.set()
        self.wait_job(job["job_id"])
        second = self.action("parallel_job.read", {"job_id": job["job_id"]})
        self.assertNotIn("reused_from_checkpoint", second)
        self.assertEqual(second["result"]["status"], "succeeded")
        self.assertEqual(second["result"]["result"]["actual_result"], 42)

    def test_dynamic_facts_refresh_and_capture_submission_remains_idempotent(self):
        for action in ["scenario_capture.catalog", "scenario_capture.read_run", "scenario_capture.discover_devices",
                       "parallel_modeling.catalog", "parallel_job.read", "parallel_job.cancel"]:
            self.assertFalse(self.router.can_reuse(action, {}, {}, self.run), action)
        plan = self.action("scenario_capture.create_plan")["result"]
        inputs = {"plan_id": plan["plan_id"], "bridge_id": "bridge_test", "device_serial": "stub"}
        with patch.object(self.service.capture, "execute", return_value={"status": "succeeded"}) as execute:
            first = self.action("scenario_capture.execute", inputs)
            self.wait_job(first["result"]["job_id"])
            second = self.action("scenario_capture.execute", inputs)
            self.assertEqual(first["result"]["job_id"], second["result"]["job_id"])
            self.assertTrue(second["reused_from_checkpoint"])
            self.assertEqual(execute.call_count, 1)
        with patch.object(self.service.capture, "discover_devices", side_effect=[{"devices": []}, {"devices": [{"serial": "stub"}]}]) as discover:
            self.action("scenario_capture.discover_devices", {"bridge_id": "bridge_test"})
            current = self.action("scenario_capture.discover_devices", {"bridge_id": "bridge_test"})
            self.assertEqual(current["result"]["devices"], [{"serial": "stub"}])
            self.assertEqual(discover.call_count, 2)

    def test_local_capture_mcp_tools_work_without_bridge_parameters(self):
        plan = self.action("scenario_capture.create_plan")["result"]
        with patch.object(self.mcp, "RUN_ID", self.run["run_id"]), \
             patch.object(self.mcp, "_request", side_effect=self.mcp_request), \
             patch.object(self.service.capture, "catalog", return_value={"local_hdc": {"available": True}}), \
             patch.object(self.service.capture, "discover_devices", return_value={"devices": ["local_stub"]}) as discover, \
             patch.object(self.service.capture, "execute", return_value={"status": "completed"}) as execute:
            discovery_tool = self.mcp.mcp._tool_manager.get_tool("scenario_capture_discover_devices")
            found = asyncio.run(discovery_tool.run({"connection_mode": "local"}, convert_result=False))
            self.assertEqual(found["status"], "succeeded", found)
            self.assertEqual(found["result"]["devices"], ["local_stub"])
            discover.assert_called_once_with("", connection_mode="local")
            execute_tool = self.mcp.mcp._tool_manager.get_tool("scenario_capture_execute")
            submitted = asyncio.run(execute_tool.run({"plan_id": plan["plan_id"], "device_serial": "local_stub", "connection_mode": "local"}, convert_result=False))
            self.assertEqual(submitted["status"], "succeeded", submitted)
            job = self.wait_job(submitted["result"]["job_id"])
            self.assertEqual(job["inputs"]["connection_mode"], "local")
            self.assertEqual(execute.call_args.kwargs["connection_mode"], "local")
            self.assertEqual(execute.call_args.kwargs["bridge_id"], "")
        self.connector.assert_not_called()

    def test_auto_capture_mcp_needs_only_a_plan_for_one_discovered_device(self):
        plan = self.action("scenario_capture.create_plan")["result"]
        with patch.object(self.mcp, "RUN_ID", self.run["run_id"]), \
             patch.object(self.mcp, "_request", side_effect=self.mcp_request), \
             patch.object(self.service.capture, "catalog", return_value={"local_hdc": {"available": True}}), \
             patch.object(self.service.gateway, "list_bridges", return_value=[]), \
             patch.object(self.service.gateway, "local_discover", return_value={"devices": [{"serial": "local_stub"}]}, create=True), \
             patch.object(self.service.capture, "execute", return_value={"status": "completed"}) as execute:
            discovery = self.mcp.scenario_capture_discover_devices()
            self.assertEqual(discovery["status"], "succeeded", discovery)
            self.assertEqual(discovery["result"]["selected_device"]["device_serial"], "local_stub")
            tool = self.mcp.mcp._tool_manager.get_tool("scenario_capture_execute")
            submitted = asyncio.run(tool.run({"plan_id": plan["plan_id"]}, convert_result=False))
            self.assertEqual(submitted["status"], "succeeded", submitted)
            job = self.wait_job(submitted["result"]["job_id"])
            self.assertEqual(job["inputs"]["connection_mode"], "local")
            self.assertEqual(execute.call_args.kwargs["device_serial"], "local_stub")
        self.connector.assert_not_called()

    def test_registered_mcp_tools_forward_to_real_runtime_and_modeling(self):
        tools = asyncio.run(self.mcp.mcp.list_tools())
        wanted = {"scenario_capture_catalog", "scenario_capture_create_plan", "scenario_capture_read_plan", "scenario_capture_generate_script",
                  "scenario_capture_discover_devices", "scenario_capture_execute", "scenario_capture_read_run", "parallel_modeling_catalog",
                  "parallel_modeling_import_dataset", "parallel_modeling_analyze", "parallel_modeling_read_analysis", "parallel_modeling_search",
                  "parallel_modeling_read_search", "parallel_job_read", "parallel_job_cancel"}
        self.assertTrue(wanted <= {tool.name for tool in tools})
        for tool in tools:
            if tool.name in wanted:
                self.assertNotIn("source_connector_id", tool.inputSchema.get("required", []))
        with patch.object(self.mcp, "RUN_ID", self.run["run_id"]), patch.object(self.mcp, "_request", side_effect=self.mcp_request):
            # Use the actual typed FastMCP registry, then its per-run action HTTP facade.
            tool = self.mcp.mcp._tool_manager.get_tool("parallel_modeling_import_dataset")
            dataset_observation = asyncio.run(tool.run({"dataset": {"builtin_id": "gallery-edit-pmu-20261014"}}, convert_result=False))
            self.assertEqual(dataset_observation["status"], "succeeded", dataset_observation)
            dataset = dataset_observation["result"]
            first = self.mcp.parallel_modeling_analyze(dataset["id"])
            self.assertEqual(first["status"], "succeeded", first)
            self.wait_job(first["result"]["job_id"])
            analyzed = self.mcp.parallel_job_read(first["result"]["job_id"])["result"]["result"]
            self.assertAlmostEqual(analyzed["baseline"]["duration_ms"], 86.94)
            second = self.mcp.parallel_modeling_analyze(dataset["id"])
            self.assertTrue(second["reused_from_checkpoint"])
            self.assertEqual(first["result"]["job_id"], second["result"]["job_id"])
            search = self.mcp.parallel_modeling_search({"analysis_id": analyzed["id"], "space": {
                "frequency_policy": "max_opps", "worker_counts": [3], "available_core_group_ids": ["big", "middle"]}})
            self.wait_job(search["result"]["job_id"])
            searched = self.mcp.parallel_job_read(search["result"]["job_id"])["result"]["result"]
            self.assertAlmostEqual(searched["best"]["metrics"]["finish_ms"], 53.87303045981391)
            self.assertEqual(self.mcp.parallel_modeling_read_search(searched["id"])["result"]["id"], searched["id"])
        self.connector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
