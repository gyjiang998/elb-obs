"""Agent-facing lease guidance and MCP forwarding; no device or Agent calls."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

os.environ.setdefault("HETERO_AGENT_RUN_ID", "run_lease_guidance_test")
os.environ.setdefault("HETERO_AGENT_TOOL_TOKEN", "lease-guidance-test-token")

from backend import kimi_mcp_server
from backend.skill_versions import DEFAULT_SKILL_PROFILE_ID, resolve_server_skill_profile, skill_prompt

ROOT = Path(__file__).resolve().parents[2]
REMOTE_BRANCH = "device.list_bridges -> device.discover -> device.probe"


class DeviceLeaseGuidanceTests(unittest.TestCase):
    def test_recommended_skill_uses_optional_lease_and_keeps_historical_source(self):
        profile = resolve_server_skill_profile(DEFAULT_SKILL_PROFILE_ID)
        self.assertEqual(profile["skills"]["agent-native-device-experimentation"]["version"], "4.5.1")
        prompt = skill_prompt(DEFAULT_SKILL_PROFILE_ID, "device_validation")
        self.assertIn(REMOTE_BRANCH, prompt)
        self.assertIn("可省略 `device_lease_id`", prompt)
        self.assertIn("可选兼容接口", prompt)
        self.assertIn("多个候选先确认目标", prompt)
        self.assertIn("任务状态未知", prompt)
        self.assertIn("不循环换租", prompt)
        historical = (ROOT / "agent-resources/skill-library/skills/agent-native-device-experimentation/4.5.0.md").read_text()
        self.assertIn("device.discover -> device.create_lease -> device.probe", historical)

    def test_native_and_compatibility_prompts_agree_on_lease_ownership(self):
        module = ast.parse((ROOT / "backend/kimi_agent_runtime.py").read_text())
        prompts = [ast.literal_eval(item.value) for item in module.body if isinstance(item, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id in {"AGENTS_MD", "DEVICE_SKILL_MD"} for target in item.targets)]
        # Evaluate the real pure prompt builder without importing app/services.
        app = ast.parse((ROOT / "backend/app.py").read_text())
        function = next(item for item in app.body if isinstance(item, ast.FunctionDef) and item.name == "_runtime_system_prompt")
        namespace = {"Any": Any, "json": json, "action_catalog": lambda: [], "RUNTIME_ACTIONS": {}, "LEGACY_BROWSER_DEVICE_ACTIONS": set()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "app_prompt", "exec"), namespace)
        prompts.append(namespace["_runtime_system_prompt"]({"mode": "device_validation"}))
        self.assertEqual(len(prompts), 3)
        for prompt in prompts:
            with self.subTest(prompt=prompt[:40]):
                self.assertIn(REMOTE_BRANCH, prompt)
                self.assertNotIn("device.discover -> device.create_lease -> device.probe", prompt)
                self.assertIn("device_lease_id", prompt)
                self.assertIn("可省略", prompt)
                self.assertIn("自动创建或复用", prompt)
                self.assertIn("对账", prompt)

    def test_mcp_catalog_exposes_optional_lease_for_confirmed_remote_target(self):
        catalog = {tool.name: tool for tool in asyncio.run(kimi_mcp_server.mcp.list_tools())}
        for name in ("device_probe", "device_deploy_artifact", "device_execute_shell", "device_collect_artifact"):
            with self.subTest(name=name):
                tool = catalog[name]
                self.assertNotIn("device_lease_id", tool.inputSchema.get("required", []))
                self.assertIn("device_lease_id 可省略", tool.description)
                self.assertIn("bridge_id", tool.description)
                self.assertIn("logical_device_id", tool.description)
        self.assertIn("可选兼容接口", catalog["device_create_lease"].description)

    def test_mcp_forwards_confirmed_identity_without_inventing_lease_and_preserves_explicit_lease(self):
        cases = [
            (kimi_mcp_server.device_probe, ("phone-real",), "device.probe"),
            (kimi_mcp_server.device_deploy_artifact, ("art_build", "art_probe"), "device.deploy_artifact"),
            (kimi_mcp_server.device_execute_shell, ("art_probe", "uname -a"), "device.execute_shell"),
            (kimi_mcp_server.device_collect_artifact, ("art_probe", "/data/local/tmp/result.txt"), "device.collect_artifact"),
        ]
        for function, args, action_id in cases:
            for explicit in (False, True):
                with self.subTest(action=action_id, explicit=explicit), patch.object(kimi_mcp_server, "_action", return_value={"status": "succeeded"}) as dispatch:
                    identity = {"bridge_id": "bridge-confirmed", "logical_device_id": "logical-discovered"}
                    if explicit: identity["device_lease_id"] = "lease-explicit"
                    function(*args, **identity)
                    observed_id, payload = dispatch.call_args.args
                    self.assertEqual(observed_id, action_id)
                    self.assertEqual(payload["bridge_id"], "bridge-confirmed")
                    self.assertEqual(payload["logical_device_id"], "logical-discovered")
                    self.assertEqual(payload.get("device_lease_id"), "lease-explicit" if explicit else None)
                    self.assertEqual(dispatch.call_count, 1)


if __name__ == "__main__": unittest.main()
