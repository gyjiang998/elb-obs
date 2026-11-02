from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.core import PlatformError
from backend.skill_versions import (
    DEFAULT_SKILL_PROFILE_ID,
    apply_skill_prompt,
    append_skill_overlay,
    activate_user_skill_version,
    resolve_skill_profile,
    resolve_server_skill_profile,
    save_user_skill_version,
    set_user_skill_store_for_testing,
    skill_management_catalog,
    skill_management_detail,
    skill_prompt,
    skill_version_library,
)
from backend.user_skills import UserSkillStore


class SkillVersionLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_store = set_user_skill_store_for_testing(
            UserSkillStore(Path(self.temporary.name) / "user-skills")
        )

    def tearDown(self) -> None:
        set_user_skill_store_for_testing(self.previous_store)
        self.temporary.cleanup()

    def test_library_exposes_stable_and_recommended_adaptive_profiles(self) -> None:
        library = skill_version_library()
        profiles = {item["profile_id"]: item for item in library["profiles"]}

        self.assertEqual(library["default_profile_id"], DEFAULT_SKILL_PROFILE_ID)
        self.assertIn("stable-2026-08-13", profiles)
        self.assertIn("adaptive-2026-08-14", profiles)
        self.assertIn("agent-centric-page-semantic-code-2026-08-28", profiles)
        self.assertIn(DEFAULT_SKILL_PROFILE_ID, profiles)
        self.assertFalse(profiles["stable-2026-08-13"]["recommended"])
        self.assertFalse(profiles["adaptive-2026-08-14"]["recommended"])
        self.assertFalse(profiles["agent-centric-page-semantic-code-2026-08-28"]["recommended"])
        self.assertTrue(profiles[DEFAULT_SKILL_PROFILE_ID]["recommended"])
        self.assertEqual(
            profiles["agent-centric-page-semantic-code-2026-08-28"]["skills"]["code-knowledge-analysis"]["version"],
            "1.6.0",
        )
        self.assertEqual(
            profiles[DEFAULT_SKILL_PROFILE_ID]["skills"]["agent-runtime-orchestrator"]["version"],
            "2.6.0",
        )
        self.assertNotEqual(
            profiles["stable-2026-08-13"]["profile_sha256"],
            profiles[DEFAULT_SKILL_PROFILE_ID]["profile_sha256"],
        )

    def test_stable_profile_keeps_base_prompt_and_legacy_agent_profile_adds_overlay(self) -> None:
        base = "BASE"

        stable = append_skill_overlay(base, "stable-2026-08-13", "page_agent")
        adaptive = append_skill_overlay(base, "agent-centric-2026-08-19", "page_agent")

        self.assertEqual(stable, base)
        self.assertIn("reasoning.run_blind_analysis", adaptive)
        self.assertIn("retry_nonce", adaptive)
        self.assertIn("门禁分级", adaptive)
        self.assertIn("不要在路由前运行盲推理", adaptive)
        self.assertIn("真机验证", adaptive)

    def test_device_preparation_profile_updates_capture_skill_without_rewriting_parent(self) -> None:
        parent = resolve_server_skill_profile("agent-centric-page-semantic-code-2026-08-31")
        current = resolve_server_skill_profile("agent-centric-page-device-preparation-2026-10-22")
        self.assertEqual(current["parent_profile_id"], parent["profile_id"])
        self.assertFalse(parent["recommended"])
        self.assertEqual(parent["skills"]["scenario-step-capture"]["version"], "1.2.0")
        self.assertEqual(current["skills"]["scenario-step-capture"]["version"], "1.3.0")
        self.assertEqual(current["skills"]["scenario-step-capture"]["source"], "skills/scenario-step-capture/1.3.0.md")
        self.assertNotEqual(parent["profile_sha256"], current["profile_sha256"])
        for skill_id, previous in parent["skills"].items():
            if skill_id != "scenario-step-capture":
                self.assertEqual(current["skills"][skill_id]["content_sha256"], previous["content_sha256"])

    def test_device_selection_profile_preserves_other_skills_and_previous_profile(self) -> None:
        previous = resolve_server_skill_profile("agent-centric-page-device-preparation-2026-10-22")
        current = resolve_server_skill_profile(DEFAULT_SKILL_PROFILE_ID)

        self.assertFalse(previous["recommended"])
        self.assertTrue(current["recommended"])
        self.assertEqual(previous["skills"]["scenario-step-capture"]["version"], "1.3.0")
        self.assertEqual(current["skills"]["scenario-step-capture"]["version"], "1.4.0")
        self.assertEqual(current["skills"]["scenario-step-capture"]["source"], "skills/scenario-step-capture/1.4.0.md")
        self.assertNotEqual(previous["profile_sha256"], current["profile_sha256"])
        self.assertEqual(set(current["skills"]), set(previous["skills"]))
        for skill_id, entry in previous["skills"].items():
            if skill_id != "scenario-step-capture":
                self.assertEqual(current["skills"][skill_id]["version"], entry["version"])
                self.assertEqual(current["skills"][skill_id]["content_sha256"], entry["content_sha256"])

    def test_concise_profile_replaces_long_base_prompts(self) -> None:
        profile = resolve_skill_profile(DEFAULT_SKILL_PROFILE_ID)
        effective = apply_skill_prompt("LEGACY LONG BASE", DEFAULT_SKILL_PROFILE_ID, "page_agent")

        self.assertNotIn("LEGACY LONG BASE", effective)
        self.assertIn("页面动作执行", effective)
        self.assertEqual(profile["skills"]["page-agent-control"]["prompt_mode"], "replace")
        self.assertTrue(profile["skills"]["page-agent-control"]["source"].endswith("5.4.0.md"))
        self.assertIn("不要例行加载全量页面动作目录", effective)
        self.assertNotIn("Agent Session", effective)

    def test_concise_profile_versions_every_model_facing_skill(self) -> None:
        profile = resolve_skill_profile(DEFAULT_SKILL_PROFILE_ID)
        expected_slots = {
            "agent_runtime", "page_agent", "page_analysis", "device_validation",
            "application_flow", "code_context", "code_knowledge", "custom_api_kb", "hotspot_investigation", "hardware_route", "full_analysis",
            "strategy_kb_rag", "strategy_hardware_kb", "strategy_direct_baseline",
            "blind_reasoning", "blind_comparison", "experience_distillation",
            "experimental_flow_hiperf", "experimental_flow_trace",
            "scenario_capture", "parallel_modeling",
        }
        actual_slots = {entry["prompt_slot"] for entry in profile["skills"].values()}

        self.assertEqual(actual_slots, expected_slots)
        self.assertTrue(all(entry["prompt_mode"] == "replace" for entry in profile["skills"].values()))
        self.assertLess(sum(entry["content_chars"] for entry in profile["skills"].values()), 24000)
        self.assertEqual(profile["skills"]["application-execution-flow-reconstruction"]["version"], "4.0.0")
        self.assertIn("页面不再设置候选确认步骤", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("application_flow_compile_mermaid", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("application_flow_enrich_trace_anchors", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("application_flow_inspect_runtime_trace", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("application_flow_apply_runtime_trace_alignment", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("application-flow-stage-analysis-v1.schema.json", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("Agent 负责判断合理的操作边界", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("完整 `event_name`", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("该工件就是本轮已选真实 Trace", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("各层节点的匹配状态", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("工具组合", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("代码底图查询", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("暂时无法落到当前源码范围", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertIn("parent_scenario_artifact_id", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "application_flow"))
        self.assertEqual(profile["skills"]["trace-hotspot-code-investigation"]["version"], "1.2.0")
        self.assertIn("桩点所在函数自动宣布为热点", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "hotspot_investigation"))
        self.assertIn("code-context-retrieval", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "hotspot_investigation"))
        self.assertIn("platform_read_artifact", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "hotspot_investigation"))
        self.assertIn("ui_execute_registered_action", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "hotspot_investigation"))
        self.assertIn("平台代码底图", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "code_context"))
        self.assertIn("原生源码工具", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "code_context"))
        self.assertIn("HiTrace 候选", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "code_context"))
        self.assertIn("trace-hotspot-code-investigation", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "agent_runtime"))
        self.assertIn("code-context-retrieval", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "agent_runtime"))
        self.assertEqual(profile["skills"]["code-context-retrieval"]["version"], "1.3.0")
        self.assertEqual(profile["skills"]["code-knowledge-analysis"]["version"], "1.7.0")
        self.assertEqual(profile["skills"]["hardware-semantic-routing-experiment"]["version"], "3.2.0")
        self.assertEqual(profile["skills"]["full-optimization-analysis"]["version"], "2.2.0")
        self.assertIn("平台静态代码底图", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "code_knowledge"))
        self.assertIn("分别标注平台底图候选", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "code_knowledge"))
        self.assertIn("code_graph_publish_report", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "code_knowledge"))
        self.assertIn("HiPerf", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "experimental_flow_hiperf"))
        self.assertIn("自由 Mermaid", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "experimental_flow_trace"))
        self.assertNotIn("application-flow-scenario/v2", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "experimental_flow_hiperf"))

    def test_agent_centric_profiles_keep_session_topology_out_of_task_prompts(self) -> None:
        profile = resolve_skill_profile(DEFAULT_SKILL_PROFILE_ID)
        runtime_prompt = skill_prompt(DEFAULT_SKILL_PROFILE_ID, "agent_runtime")
        device_prompt = skill_prompt(DEFAULT_SKILL_PROFILE_ID, "device_validation")

        self.assertEqual(profile["skills"]["agent-runtime-orchestrator"]["version"], "2.6.0")
        self.assertEqual(profile["skills"]["page-agent-control"]["version"], "5.4.0")
        self.assertEqual(profile["skills"]["page-analysis-artifact-orchestrator"]["version"], "3.3.0")
        self.assertEqual(profile["skills"]["agent-native-device-experimentation"]["version"], "4.5.1")
        self.assertEqual(profile["skills"]["blind-opportunity-analysis"]["version"], "2.1.0")
        self.assertIn("reasoning_prepare_hardware_semantic_route", runtime_prompt)
        self.assertIn("$HETERO_AGENT_SKILL_ROOT/<skill-id>/SKILL.md", runtime_prompt)
        self.assertIn(".agents/skills/<skill-id>/SKILL.md", runtime_prompt)
        self.assertIn("Agent 框架自身的 Skill 发现约定", runtime_prompt)
        self.assertIn("页面命令已点名 MCP 工具时立即调用", runtime_prompt)
        self.assertNotIn("不要先搜索平台源码、Action ID 或全量 Skill", runtime_prompt)
        self.assertIn("后续阶段沿用已经加载的内容", runtime_prompt)
        self.assertNotIn("清单与哈希", runtime_prompt)
        self.assertNotIn("platform_ack_skill_bootstrap", runtime_prompt)
        self.assertIn("真实 MCP 工具", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "page_analysis"))
        self.assertIn("不得为调用它们先执行 `ls`、`grep`", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "page_analysis"))
        self.assertIn("真实结果", runtime_prompt + device_prompt)
        self.assertIn("设备分支", device_prompt)
        self.assertIn("构建分支", device_prompt)
        self.assertIn("device.create_lease", device_prompt)
        self.assertIn("device.list_bridges -> device.discover -> device.probe", device_prompt)
        self.assertNotIn("device.discover -> device.create_lease -> device.probe", device_prompt)
        self.assertIn("可省略 `device_lease_id`", device_prompt)
        self.assertIn("可选兼容接口", device_prompt)
        self.assertIn("任务状态未知", device_prompt)
        self.assertIn("对账", device_prompt)
        self.assertIn("租约是平台并发控制状态", device_prompt)
        self.assertIn("lease_resolution", device_prompt)
        self.assertIn("device.deploy_artifact", device_prompt)
        self.assertNotIn("互不等待", device_prompt)
        self.assertNotIn("Kimi 原生", runtime_prompt + device_prompt)

        forbidden = (
            "Agent Session", "Kimi Session", "当前 Session", "同一 Session", "本 Session",
            "另起模型会话", "新模型会话", "另一模型会话", "代为触发", "由你在同一",
        )
        for listed in skill_version_library()["profiles"]:
            profile_id = listed["profile_id"]
            if not profile_id.startswith("agent-centric-"):
                continue
            resolved = resolve_server_skill_profile(profile_id)
            for skill_id, entry in resolved["skills"].items():
                with self.subTest(profile_id=profile_id, skill_id=skill_id):
                    prompt = entry.get("prompt") or entry.get("addendum") or ""
                    for phrase in forbidden:
                        self.assertNotIn(phrase, prompt)

    def test_blind_prompts_are_available_in_both_profiles(self) -> None:
        for profile_id in ("stable-2026-08-13", "adaptive-2026-08-14", DEFAULT_SKILL_PROFILE_ID):
            self.assertIn("独立盲推理", skill_prompt(profile_id, "blind_reasoning"))
            self.assertTrue(
                "同模型自评" in skill_prompt(profile_id, "blind_comparison")
                or "同模型诊断" in skill_prompt(profile_id, "blind_comparison")
            )

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaises(PlatformError) as raised:
            resolve_skill_profile("missing-profile")
        self.assertEqual(raised.exception.code, "SKILL_PROFILE_UNKNOWN")

    def test_user_edit_creates_immutable_version_and_overrides_server_at_resolution_time(self) -> None:
        server = resolve_server_skill_profile(DEFAULT_SKILL_PROFILE_ID)
        original = server["skills"]["strategy-hardware-kb"]

        first = save_user_skill_version(
            skill_id="strategy-hardware-kb",
            label_zh="硬件机会推理",
            description_zh="用户精简版",
            content="# 用户版本\n\n保留语义匹配和证据边界。",
            profile_id=DEFAULT_SKILL_PROFILE_ID,
            change_summary_zh="精简冗余约束",
        )
        second = save_user_skill_version(
            skill_id="strategy-hardware-kb",
            label_zh="硬件机会推理",
            description_zh="用户精简版二",
            content="# 用户版本二\n\n先识别计算核心，再检索 API。",
            profile_id=DEFAULT_SKILL_PROFILE_ID,
        )

        self.assertNotEqual(first["version"]["version_id"], second["version"]["version_id"])
        effective = resolve_skill_profile(DEFAULT_SKILL_PROFILE_ID)["skills"]["strategy-hardware-kb"]
        self.assertEqual(effective["origin"], "user")
        self.assertEqual(effective["user_version_id"], second["version"]["version_id"])
        self.assertIn("先识别计算核心", skill_prompt(DEFAULT_SKILL_PROFILE_ID, "strategy_hardware_kb"))
        self.assertEqual(
            resolve_server_skill_profile(DEFAULT_SKILL_PROFILE_ID)["skills"]["strategy-hardware-kb"]["content_sha256"],
            original["content_sha256"],
        )
        catalog = skill_management_catalog(DEFAULT_SKILL_PROFILE_ID)
        item = next(item for item in catalog["skills"] if item["skill_id"] == "strategy-hardware-kb")
        self.assertEqual(item["user_version_count"], 2)
        self.assertEqual(item["effective_origin"], "user")

        activate_user_skill_version("strategy-hardware-kb", None, profile_id=DEFAULT_SKILL_PROFILE_ID)
        restored = resolve_skill_profile(DEFAULT_SKILL_PROFILE_ID)["skills"]["strategy-hardware-kb"]
        self.assertEqual(restored["origin"], "server")
        self.assertEqual(restored["content_sha256"], original["content_sha256"])

    def test_agent_can_create_and_hot_activate_new_user_only_skill(self) -> None:
        saved = save_user_skill_version(
            skill_id="inspect-cache-locality",
            label_zh="缓存局部性分析",
            description_zh="分析连续、跨步和随机访存。",
            content="# 缓存局部性分析\n\n识别访问步长、复用距离与写回模式。",
            profile_id=DEFAULT_SKILL_PROFILE_ID,
            created_by="page_agent",
        )
        effective = resolve_skill_profile(DEFAULT_SKILL_PROFILE_ID)
        custom = effective["skills"]["inspect-cache-locality"]
        self.assertEqual(custom["origin"], "user")
        self.assertEqual(custom["prompt_slot"], "custom:inspect-cache-locality")
        detail = skill_management_detail(
            "inspect-cache-locality",
            profile_id=DEFAULT_SKILL_PROFILE_ID,
            version_id=saved["version"]["version_id"],
        )
        self.assertTrue(detail["effective"])
        self.assertIn("复用距离", detail["skill"]["content"])


if __name__ == "__main__":
    unittest.main()
