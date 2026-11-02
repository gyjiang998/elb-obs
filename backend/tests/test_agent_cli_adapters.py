from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.agent_cli_adapters import AgentCliAdapterError, AgentCliAdapterRegistry
from backend.skill_bootstrap import build_skill_bootstrap_requirement


class AgentCliAdapterRegistryTests(unittest.TestCase):
    def _registry(self, root: Path, manifest: Path | None = None) -> AgentCliAdapterRegistry:
        kimi = root / "kimi"
        kimi.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        kimi.chmod(0o755)
        return AgentCliAdapterRegistry(
            kimi_binary=kimi,
            kimi_environment_factory=lambda: os.environ.copy(),
            custom_manifest_path=manifest,
            default_adapter_id="kimi",
            user_skill_source_roots=(),
        )

    def test_catalog_exposes_builtins_and_custom_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            custom = root / "team-agent"
            custom.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            custom.chmod(0o755)
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({
                "schema_version": "agent_cli_adapter_catalog/v1",
                "adapters": [{
                    "adapter_id": "team-agent",
                    "display_name": "Team Agent",
                    "executable": str(custom),
                    "args": ["--workspace", "{workspace}"],
                    "resume_args": ["--resume", "{agent_session_id}"],
                    "session_strategy": "native_session",
                    "capabilities": {"mcp": True, "skills": True},
                    "workspace": {"skill_roots": [".team/skills"], "mcp_format": "kimi", "mcp_path": ".team/mcp.json"},
                }],
            }), encoding="utf-8")

            registry = self._registry(root, manifest)
            catalog = registry.catalog()

            self.assertEqual({item["adapter_id"] for item in catalog["adapters"]}, {"kimi", "opencode", "team-agent"})
            self.assertTrue(registry.get("team-agent").public()["available"])
            self.assertEqual(registry.get("team-agent").public()["startup_input_delay_ms"], 600)
            self.assertEqual(registry.get("team-agent").public()["submit_delay_ms"], 35)
            self.assertEqual(registry.get("team-agent").public()["submit_fallback_ms"], 0)
            self.assertIn("HETERO_AGENT_MCP_CONFIG", registry.get("team-agent").public()["mcp_integration"]["warning"])
            self.assertTrue(registry.get("kimi").public()["browser_model_override_supported"])

    def test_builtin_kimi_launch_uses_explicit_run_skill_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            adapter = self._registry(root).get("kimi")

            launch = adapter.build_launch(
                workspace=workspace,
                run_id="run_1",
                agent_session_id="session_1",
                agent_workspace_id="workspace_1",
                resume=False,
            )

            self.assertEqual(
                launch.command[1:],
                (
                    "--auto",
                    "--skills-dir",
                    str((workspace / ".agents" / "skills").resolve()),
                    "--session",
                    "session_1",
                ),
            )
            self.assertEqual(
                launch.environment["HETERO_AGENT_SKILL_ROOT"],
                str((workspace / ".agents" / "skills").resolve()),
            )

    def test_custom_submit_framing_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "line-feed-agent",
                "display_name": "Line Feed Agent",
                "executable": str(binary),
                "submit_mode": "bracketed_paste_lf",
                "startup_input_delay_ms": 450,
                "submit_delay_ms": 1600,
                "submit_fallback_ms": 220,
            }]}), encoding="utf-8")

            adapter = self._registry(root, manifest).get("line-feed-agent")
            launch = adapter.build_launch(
                workspace=root,
                run_id="run_1",
                agent_session_id="session_1",
                agent_workspace_id="workspace_1",
                resume=False,
            )

            self.assertEqual(launch.submit_mode, "bracketed_paste_lf")
            self.assertEqual(launch.startup_input_delay_ms, 450)
            self.assertEqual(launch.submit_delay_ms, 1600)
            self.assertEqual(launch.submit_fallback_ms, 220)

    def test_standard_usage_journal_is_exposed_to_custom_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "usage-agent",
                "display_name": "Usage Agent",
                "executable": str(binary),
                "usage_kind": "standard_jsonl",
                "capabilities": {"token_usage": True},
            }]}), encoding="utf-8")
            adapter = self._registry(root, manifest).get("usage-agent")

            launch = adapter.build_launch(
                workspace=root / "workspace",
                run_id="run_1",
                agent_session_id="session_1",
                agent_workspace_id="workspace_1",
                resume=False,
            )

            expected = (root / "workspace" / ".agent-platform" / "token-usage.jsonl").resolve()
            self.assertEqual(launch.environment["HETERO_AGENT_USAGE_JSONL"], str(expected))
            self.assertEqual(adapter.public()["usage_kind"], "standard_jsonl")
            self.assertTrue(adapter.public()["capabilities"]["token_usage"])

    def test_custom_launch_receives_explicit_mcp_paths_and_standard_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            (root / ".agent-platform").mkdir()
            (root / ".agent-platform" / "mcp.json").write_text('{"servers":{}}\n', encoding="utf-8")
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-agent",
                "display_name": "Team Agent",
                "executable": str(binary),
                "args": ["--mcp-config", "{mcp_config}", "--skills", "{skill_root}"],
                "workspace": {"mcp_format": "kimi", "mcp_path": ".team/mcp.json"},
                "capabilities": {"mcp": True},
            }]}), encoding="utf-8")
            adapter = self._registry(root, manifest).get("team-agent")
            adapter.materialize_workspace(root)

            launch = adapter.build_launch(
                workspace=root,
                run_id="run_1",
                agent_session_id="session_1",
                agent_workspace_id="workspace_1",
                resume=False,
            )

            native_config = str((root / ".team" / "mcp.json").resolve())
            self.assertEqual(launch.command[1:3], ("--mcp-config", native_config))
            self.assertEqual(launch.environment["HETERO_AGENT_MCP_CONFIG"], native_config)
            self.assertEqual(launch.environment["HETERO_AGENT_MCP_DESCRIPTOR"], str((root / ".agent-platform" / "mcp.json").resolve()))
            metadata = json.loads((root / ".agent-platform" / "adapter.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["mcp"]["native_config"], ".team/mcp.json")
            self.assertEqual(adapter.public()["mcp_integration"]["delivery"], "argv_and_environment")

    def test_native_projection_replaces_stale_platform_server_and_keeps_external_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            platform_dir = root / ".agent-platform"
            platform_dir.mkdir()
            (platform_dir / "mcp.json").write_text(json.dumps({"servers": {
                "heterogeneous_platform_new": {
                    "transport": "stdio",
                    "command": ["python3", "-m", "backend.kimi_mcp_server"],
                },
            }}), encoding="utf-8")
            native = root / ".team" / "mcp.json"
            native.parent.mkdir()
            native.write_text(json.dumps({"mcpServers": {
                "heterogeneous_platform_old": {"command": "old"},
                "external_server": {"command": "external"},
            }}), encoding="utf-8")
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-agent",
                "display_name": "Team Agent",
                "executable": str(binary),
                "workspace": {"mcp_format": "kimi", "mcp_path": ".team/mcp.json"},
                "capabilities": {"mcp": True},
            }]}), encoding="utf-8")

            adapter = self._registry(root, manifest).get("team-agent")
            adapter.materialize_workspace(root)
            projected = json.loads(native.read_text(encoding="utf-8"))["mcpServers"]
            projection = json.loads((platform_dir / "adapter-team-agent-projection.json").read_text(encoding="utf-8"))

            self.assertEqual(set(projected), {"heterogeneous_platform_new", "external_server"})
            self.assertNotIn("mtime_ns", projection["neutral_mcp"])

    def test_launch_contract_changes_when_mcp_projection_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = self._registry(root).get("kimi")
            native = root / ".kimi-code" / "mcp.json"
            native.parent.mkdir(parents=True)
            native.write_text('{"mcpServers":{"first":{}}}\n', encoding="utf-8")
            first = adapter.build_launch(
                workspace=root,
                run_id="run_1",
                agent_session_id="session_1",
                agent_workspace_id="workspace_1",
                resume=False,
            )
            native.write_text('{"mcpServers":{"second":{}}}\n', encoding="utf-8")
            second = adapter.build_launch(
                workspace=root,
                run_id="run_1",
                agent_session_id="session_1",
                agent_workspace_id="workspace_1",
                resume=False,
            )
            native.write_text('{"mcpServers":{"second":{}}}\n', encoding="utf-8")
            same_content = adapter.build_launch(
                workspace=root,
                run_id="run_1",
                agent_session_id="session_1",
                agent_workspace_id="workspace_1",
                resume=False,
            )

            self.assertNotEqual(first.workspace_contract_sha256, second.workspace_contract_sha256)
            self.assertEqual(second.workspace_contract_sha256, same_content.workspace_contract_sha256)

    def test_mcp_config_placeholder_requires_native_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "bad-mcp-agent",
                "display_name": "Bad MCP Agent",
                "executable": str(binary),
                "args": ["--mcp-config", "{mcp_config}"],
            }]}), encoding="utf-8")

            with self.assertRaises(AgentCliAdapterError) as captured:
                self._registry(root, manifest)
            self.assertEqual(captured.exception.code, "AGENT_CLI_MCP_CONFIG_MISSING")

    def test_custom_launch_is_argv_only_and_expands_known_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-agent",
                "display_name": "Team Agent",
                "executable": str(binary),
                "args": ["--root", "{workspace}", "--run", "{run_id}"],
                "resume_args": ["--resume", "{agent_session_id}"],
                "session_strategy": "native_session",
            }]}), encoding="utf-8")
            adapter = self._registry(root, manifest).get("team-agent")

            launch = adapter.build_launch(
                workspace=root,
                run_id="run_1",
                agent_session_id="session_1",
                agent_workspace_id="workspace_1",
                resume=True,
            )

            self.assertEqual(launch.command, (str(binary.resolve()), "--root", str(root.resolve()), "--run", "run_1", "--resume", "session_1"))
            self.assertNotIn("shell", " ".join(launch.command).casefold())

    def test_unknown_placeholder_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "bad-agent",
                "display_name": "Bad Agent",
                "executable": str(binary),
                "args": ["{arbitrary_command}"],
            }]}), encoding="utf-8")

            with self.assertRaises(AgentCliAdapterError) as captured:
                self._registry(root, manifest)
            self.assertEqual(captured.exception.code, "AGENT_CLI_PLACEHOLDER_INVALID")

    def test_tolerant_registry_keeps_last_valid_manifest_and_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-agent",
                "display_name": "Team Agent",
                "executable": str(binary),
            }]}), encoding="utf-8")
            kimi = root / "kimi"
            kimi.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            kimi.chmod(0o755)
            registry = AgentCliAdapterRegistry(
                kimi_binary=kimi,
                kimi_environment_factory=lambda: os.environ.copy(),
                custom_manifest_path=manifest,
                tolerate_manifest_errors=True,
                user_skill_source_roots=(),
            )

            manifest.write_text('{"adapters":[{"adapter_id":"bad"', encoding="utf-8")
            catalog = registry.catalog()

            self.assertIsNotNone(catalog["manifest_error"])
            self.assertEqual(catalog["manifest_error"]["code"], "AGENT_CLI_MANIFEST_INVALID")
            self.assertEqual(registry.get("team-agent").adapter_id, "team-agent")

    def test_opencode_projection_contains_portable_skills_and_mcp_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".agents" / "skills" / "sample").mkdir(parents=True)
            (root / ".agents" / "skills" / "sample" / "SKILL.md").write_text("---\nname: sample\ndescription: test\n---\n", encoding="utf-8")
            (root / ".agent-platform").mkdir()
            (root / ".agent-platform" / "mcp.json").write_text(json.dumps({
                "servers": {
                    "platform": {
                        "command": ["python3", "-m", "backend.kimi_mcp_server"],
                        "cwd": str(root),
                        "environment": {"HETERO_AGENT_TOOL_TOKEN": "run-scoped-token"},
                        "tool_timeout_ms": 900000,
                    }
                }
            }), encoding="utf-8")
            registry = self._registry(root)
            adapter = registry.get("opencode")

            adapter.materialize_workspace(root)

            config = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
            self.assertEqual(config["mcp"]["platform"]["type"], "local")
            self.assertEqual(config["permission"]["webfetch"], "deny")
            self.assertTrue((root / ".opencode" / "skills" / "sample" / "SKILL.md").is_file())

    def test_custom_native_skill_projection_is_refreshed_from_neutral_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            neutral_skill = root / ".agents" / "skills" / "runtime" / "SKILL.md"
            neutral_skill.parent.mkdir(parents=True)
            neutral_skill.write_text("# Runtime v1\n", encoding="utf-8")
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-agent",
                "display_name": "Team Agent",
                "executable": str(binary),
                "workspace": {"skill_roots": [".team/skills"]},
            }]}), encoding="utf-8")
            adapter = self._registry(root, manifest).get("team-agent")

            adapter.materialize_workspace(root)
            projected = root / ".team" / "skills" / "runtime" / "SKILL.md"
            self.assertEqual(projected.read_text(encoding="utf-8"), "# Runtime v1\n")

            neutral_skill.write_text("# Runtime v2\n", encoding="utf-8")
            adapter.materialize_workspace(root)

            self.assertEqual(projected.read_text(encoding="utf-8"), "# Runtime v2\n")

    def test_unchanged_native_projection_is_not_copied_for_every_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            neutral_skill = root / ".agents" / "skills" / "runtime" / "SKILL.md"
            neutral_skill.parent.mkdir(parents=True)
            neutral_skill.write_text("# Stable runtime\n", encoding="utf-8")
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-agent",
                "display_name": "Team Agent",
                "executable": str(binary),
                "workspace": {"skill_roots": [".team/skills"]},
            }]}), encoding="utf-8")
            adapter = self._registry(root, manifest).get("team-agent")

            adapter.materialize_workspace(root)
            with patch("backend.agent_cli_adapters.shutil.copytree") as copytree:
                adapter.materialize_workspace(root)

            copytree.assert_not_called()
            self.assertTrue((root / ".agent-platform" / "adapter-team-agent-projection.json").is_file())

    def test_external_skill_source_is_imported_into_neutral_and_native_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            source_root = root / "installed-skills"
            source_skill = source_root / "team-code-analysis"
            source_skill.mkdir(parents=True)
            (source_skill / "SKILL.md").write_text("# Team code analysis\n", encoding="utf-8")
            (source_skill / "helper.txt").write_text("integration\n", encoding="utf-8")
            workspace = root / "workspace"
            (workspace / ".agents" / "skills" / "runtime").mkdir(parents=True)
            (workspace / ".agents" / "skills" / "runtime" / "SKILL.md").write_text("# Runtime\n", encoding="utf-8")
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-agent",
                "display_name": "Team Agent",
                "executable": str(binary),
                "workspace": {
                    "skill_roots": [".agents/skills", ".team/skills"],
                    "skill_source_roots": [str(source_root)],
                },
            }]}), encoding="utf-8")
            adapter = self._registry(root, manifest).get("team-agent")

            adapter.materialize_workspace(workspace)

            neutral = workspace / ".agents" / "skills" / "team-code-analysis" / "SKILL.md"
            native = workspace / ".team" / "skills" / "team-code-analysis" / "SKILL.md"
            self.assertEqual(neutral.read_text(encoding="utf-8"), "# Team code analysis\n")
            self.assertEqual(native.read_text(encoding="utf-8"), "# Team code analysis\n")
            metadata = json.loads((workspace / ".agent-platform" / "adapter.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["skills"]["imported"][0]["skill_id"], "team-code-analysis")
            self.assertTrue(adapter.public()["skill_integration"]["source_roots"][0]["available"])
            requirement = build_skill_bootstrap_requirement(
                workspace=workspace,
                run={"context": {"skill_profile": {"profile_id": "test"}}},
                adapter_id=adapter.adapter_id,
                adapter_fingerprint="adapter-hash",
                agent_session_id="session-1",
                executable=adapter.resolve_executable(),
            )
            imported_inventory = next(
                item for item in requirement["inventory"] if item["skill_id"] == "team-code-analysis"
            )
            self.assertEqual(imported_inventory["source"], ".agents/skills/team-code-analysis/SKILL.md")
            self.assertEqual(requirement["target_skills"], {})

    def test_user_home_agents_skills_are_a_default_external_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            source_skill = home / ".agents" / "skills" / "team-code-analysis"
            source_skill.mkdir(parents=True)
            (source_skill / "SKILL.md").write_text("# Team code analysis\n", encoding="utf-8")
            kimi = root / "kimi"
            kimi.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            kimi.chmod(0o755)
            workspace = root / "workspace"

            with patch.dict(os.environ, {"HOME": str(home)}):
                registry = AgentCliAdapterRegistry(
                    kimi_binary=kimi,
                    kimi_environment_factory=lambda: os.environ.copy(),
                )
                adapter = registry.get("kimi")
                adapter.materialize_workspace(workspace)

            projected = workspace / ".agents" / "skills" / "team-code-analysis" / "SKILL.md"
            native = workspace / ".kimi-code" / "skills" / "team-code-analysis" / "SKILL.md"
            self.assertEqual(projected.read_text(encoding="utf-8"), "# Team code analysis\n")
            self.assertEqual(native.read_text(encoding="utf-8"), "# Team code analysis\n")
            metadata = json.loads(
                (workspace / ".agent-platform" / "external-skill-imports.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["source_roots"][0]["configured"], "~/.agents/skills")
            self.assertEqual(metadata["imported"][0]["skill_id"], "team-code-analysis")

    def test_custom_adapter_inherits_user_home_skill_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            source_skill = home / ".agents" / "skills" / "team-code-analysis"
            source_skill.mkdir(parents=True)
            (source_skill / "SKILL.md").write_text("# Team code analysis\n", encoding="utf-8")
            binary = root / "team-agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            kimi = root / "kimi"
            kimi.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            kimi.chmod(0o755)
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-agent",
                "display_name": "Team Agent",
                "executable": str(binary),
                "workspace": {"skill_roots": [".team/skills"]},
            }]}), encoding="utf-8")
            workspace = root / "workspace"

            with patch.dict(os.environ, {"HOME": str(home)}):
                registry = AgentCliAdapterRegistry(
                    kimi_binary=kimi,
                    kimi_environment_factory=lambda: os.environ.copy(),
                    custom_manifest_path=manifest,
                )
                adapter = registry.get("team-agent")
                adapter.materialize_workspace(workspace)

            projected = workspace / ".agents" / "skills" / "team-code-analysis" / "SKILL.md"
            native = workspace / ".team" / "skills" / "team-code-analysis" / "SKILL.md"
            self.assertTrue(projected.is_file())
            self.assertTrue(native.is_file())
            self.assertEqual(adapter.skill_source_roots, ("~/.agents/skills",))

    def test_platform_skill_wins_external_skill_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            source_skill = root / "installed-skills" / "runtime"
            source_skill.mkdir(parents=True)
            (source_skill / "SKILL.md").write_text("# External\n", encoding="utf-8")
            workspace = root / "workspace"
            platform_skill = workspace / ".agents" / "skills" / "runtime" / "SKILL.md"
            platform_skill.parent.mkdir(parents=True)
            platform_skill.write_text("# Platform\n", encoding="utf-8")
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-agent",
                "display_name": "Team Agent",
                "executable": str(binary),
                "workspace": {"skill_source_roots": [str(source_skill.parent)]},
            }]}), encoding="utf-8")

            self._registry(root, manifest).get("team-agent").materialize_workspace(workspace)

            self.assertEqual(platform_skill.read_text(encoding="utf-8"), "# Platform\n")
            metadata = json.loads((workspace / ".agent-platform" / "adapter.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["skills"]["collisions"][0]["resolution"], "platform_or_user_version_kept")

    def test_external_skill_import_refreshes_and_removes_stale_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            source_skill = root / "installed-skills" / "team-code-analysis"
            source_skill.mkdir(parents=True)
            source_file = source_skill / "SKILL.md"
            source_file.write_text("# V1\n", encoding="utf-8")
            workspace = root / "workspace"
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-agent",
                "display_name": "Team Agent",
                "executable": str(binary),
                "workspace": {"skill_source_roots": [str(source_skill.parent)]},
            }]}), encoding="utf-8")
            adapter = self._registry(root, manifest).get("team-agent")
            projected = workspace / ".agents" / "skills" / "team-code-analysis" / "SKILL.md"

            adapter.materialize_workspace(workspace)
            source_file.write_text("# V2\n", encoding="utf-8")
            adapter.materialize_workspace(workspace)
            self.assertEqual(projected.read_text(encoding="utf-8"), "# V2\n")

            source_skill.rename(root / "removed-team-code-analysis")
            adapter.materialize_workspace(workspace)
            self.assertFalse(projected.exists())

    def test_opencode_browser_credential_stays_in_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = self._registry(root)
            adapter = registry.get("opencode")
            if adapter.resolve_executable() is None:
                self.skipTest("OpenCode is not installed in this test environment")

            launch = adapter.build_launch(
                workspace=root,
                run_id="run_1",
                agent_session_id="session_1",
                agent_workspace_id="workspace_1",
                resume=False,
                credential={
                    "base_url": "http://model.internal/v1",
                    "api_key": "secret-value",
                    "model": "coder",
                    "context_window_tokens": 32768,
                },
            )

            self.assertEqual(launch.environment["HETERO_AGENT_MODEL_API_KEY"], "secret-value")
            self.assertNotIn("secret-value", launch.environment["OPENCODE_CONFIG_CONTENT"])
            self.assertIn("{env:HETERO_AGENT_MODEL_API_KEY}", launch.environment["OPENCODE_CONFIG_CONTENT"])


if __name__ == "__main__":
    unittest.main()
