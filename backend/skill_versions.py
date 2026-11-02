"""Source-controlled runtime profiles for model-facing Skills.

The large base prompts remain versioned engineering assets in their owning
modules.  A profile selects those immutable base versions and, when needed,
adds a small reviewed overlay.  Keeping the overlay separate makes rollback a
runtime operation instead of a source edit.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from .core import PlatformError, sha256_json
from .platform_paths import state_root
from .user_skills import UserSkillStore


DEFAULT_SKILL_PROFILE_ID = "agent-centric-page-device-selection-2026-10-26"

_SKILL_LIBRARY_ROOT = Path(__file__).resolve().parents[1] / "agent-resources" / "skill-library"
_USER_SKILL_STORE = UserSkillStore(state_root() / "user-skill-library")


def _global_server_skills() -> dict[str, dict[str, Any]]:
    definitions = {
        "scenario-step-capture": {
            "source": "skills/scenario-step-capture/1.2.0.md",
            "version": "1.2.0",
            "prompt_slot": "scenario_capture",
            "label_zh": "场景步骤采集",
            "description_zh": "将操作步骤转为 Python 自动化计划，自动发现设备并选择直连或 Bridge 采集目标步骤。",
        },
        "builditem-parallel-modeling": {
            "source": "skills/builditem-parallel-modeling/1.1.0.md",
            "version": "1.1.0",
            "prompt_slot": "parallel_modeling",
            "label_zh": "BuildItem 并行建模",
            "description_zh": "拆解关键路径、提取负载特征，使用离线模型仿真并行和核频配置及预期收益。",
        },
        "execution-pitfall-distillation": {
            "source": "skills/execution-pitfall-distillation/1.0.0.md",
            "version": "1.0.0",
            "prompt_slot": "experience_distillation",
            "label_zh": "执行踩坑提炼",
            "description_zh": "对比历史执行与环境差异，提炼可复用问题类、处置方案及 Skill 改进候选。",
        },
        "experimental-flow-hiperf-reconstruction": {
            "source": "skills/experimental-flow-hiperf-reconstruction/1.0.0.md",
            "version": "1.0.0",
            "prompt_slot": "experimental_flow_hiperf",
            "label_zh": "实验流程重建（HiPerf）",
            "description_zh": "结合源码与 HiPerf 采样生成函数级自由 Mermaid，并映射 Self 指标。",
        },
        "experimental-flow-trace-reconstruction": {
            "source": "skills/experimental-flow-trace-reconstruction/1.0.0.md",
            "version": "1.0.0",
            "prompt_slot": "experimental_flow_trace",
            "label_zh": "实验流程重建（Trace）",
            "description_zh": "结合源码与真实 Trace 生成事件校准的函数级自由 Mermaid。",
        },
    }
    skills: dict[str, dict[str, Any]] = {}
    for skill_id, definition in definitions.items():
        source = str(definition["source"])
        content = (_SKILL_LIBRARY_ROOT / source).read_text(encoding="utf-8").strip()
        skills[skill_id] = {
            **definition,
            "prompt_mode": "replace",
            "prompt": content,
        }
    return skills


def _load_source_profile(file_name: str) -> dict[str, Any]:
    profile_path = (_SKILL_LIBRARY_ROOT / "profiles" / file_name).resolve()
    try:
        profile_path.relative_to(_SKILL_LIBRARY_ROOT.resolve())
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid Skill profile source {file_name}: {exc}") from exc
    if not isinstance(profile, dict) or profile.get("schema_version") != "skill_profile_source/v1":
        raise RuntimeError(f"invalid Skill profile source contract: {file_name}")
    source_skills = profile.get("skills")
    if not isinstance(source_skills, dict) or not source_skills:
        raise RuntimeError(f"Skill profile has no skills: {file_name}")
    materialized_skills: dict[str, Any] = {}
    for skill_id, raw_entry in source_skills.items():
        if not isinstance(raw_entry, dict):
            raise RuntimeError(f"invalid Skill entry {skill_id}: {file_name}")
        source = str(raw_entry.get("source") or "").strip()
        prompt_mode = str(raw_entry.get("prompt_mode") or "append").strip()
        if prompt_mode not in {"append", "replace"}:
            raise RuntimeError(f"invalid prompt_mode for {skill_id}: {prompt_mode}")
        source_path = (_SKILL_LIBRARY_ROOT / source).resolve()
        try:
            source_path.relative_to(_SKILL_LIBRARY_ROOT.resolve())
            prompt = source_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"invalid Skill source {source}: {exc}") from exc
        if not prompt:
            raise RuntimeError(f"empty Skill source {source}")
        materialized_skills[str(skill_id)] = {
            key: deepcopy(value)
            for key, value in raw_entry.items()
            if key != "source"
        } | {
            "prompt": prompt,
            "prompt_mode": prompt_mode,
            "source": source,
        }
    return {
        key: deepcopy(value)
        for key, value in profile.items()
        if key not in {"schema_version", "skills"}
    } | {"skills": materialized_skills}


_ADAPTIVE_PAGE_AGENT = """
## 自适应分析与可逆流程补充 v1

- 新热点代码就绪后，正式硬件路由前优先调用 `reasoning.run_blind_analysis`。该调用只接收代码、用户问题和目标平台画像；失败或暂时不可用只记录缺口，不阻断正式流程。
- 路由、机会推理和真机验证都是可修订假设，不是不可逆阶段。若机会推理发现主硬件语义不相容，或真机事实否定当前 API family，可带 `reason` 与新的 `retry_nonce` 再调用 `reasoning.run_hardware_semantic_route`；新路由工件产生后重新运行对应策略。若单元仍合适但 API 不合适，优先在同一单元内重新检索或人工选择 API。
- 正式策略结果保存后，若本轮已有盲推理结果，调用 `reasoning.compare_blind_flow`。比较使用冻结的盲推结果，不得改写盲推输入或结论，也不得把同模型自评当作真实正确率。
- 门禁分级处理：结构契约、证据来源、工作站安全和真实 action 依赖是硬门禁；缺少设备、搬运、端到端或 benchmark 证据通常降低证据等级并形成验证项，不应抹去已有代码支持的局部候选。
""".strip()


_ADAPTIVE_PAGE_ANALYSIS = """
## 旁路基线与回退补充 v1

1. 热点工件形成后，优先运行一次独立盲推理基线。它是非阻塞旁路，不替代路由、知识库检索、策略结果或证据校验。
2. 路由和策略可反复修订。后续阶段否定当前单元或 API 时，生成带原因的新路由工件；新路由会使旧策略成为历史记录，而不是继续沿用。
3. 正式策略完成后，把结构化流程结果与冻结的盲推结果做对照，分别列出共识、分歧、无依据断言和合并建议。比较结果只用于诊断 Skill/知识库增益，不是真值标签。
""".strip()


_POST_VALIDATION_PAGE_AGENT = """
## 后置盲推与可逆流程补充 v2

- 新热点代码就绪后，先完成硬件路由、机会推理和可执行的真机验证闭环；不要在路由前运行盲推理。路由、API、模板与真机实验都是可修订假设，后续证据可触发同单元 API 重选，或携带 `reason` 与新的 `retry_nonce` 重新路由。
- 正式流程到达设备执行结果或明确的环境终止证据后，再调用 `reasoning.run_blind_analysis`。盲推输入只包含最初的用户问题、热点代码和目标平台画像，不得包含路由、知识库、API Top-N、Trace 结论、流程结果或真机结果。
- 盲推理结果保存后，调用 `reasoning.compare_blind_flow`，比较冻结的盲推结果与正式流程结果。比较用于识别流程资产的增益、过度约束或遗漏，不是真值标签。
- 门禁分级处理：结构契约、证据来源、工作站安全和真实 action 依赖是硬门禁；缺少设备、搬运、端到端或 benchmark 证据通常降低证据等级并形成验证项，不应抹去已有代码支持的局部候选。
""".strip()


_POST_VALIDATION_PAGE_ANALYSIS = """
## 后置盲推与结果对照补充 v2

1. 主流程顺序是热点绑定、硬件路由、机会推理和真机验证。盲推理不参与主流程决策，也不为路由预选硬件或 API。
2. 真机阶段达到真实执行结果或明确环境终止后，启动独立盲推理。其输入投影严格排除 KB、API 检索、路由、Trace 结论、流程结论、真机输出与既有分析历史。
3. 盲推理完成后，把结构化流程结果和设备证据与冻结的盲推结果对照，分别列出共识、分歧、无依据断言和合并建议。比较结果只用于诊断 Skill/知识库增益，不是真值标签。
4. 路由和策略可反复修订。后续阶段否定当前单元或 API 时，生成带原因的新路由工件；旧结果保留为历史记录。
""".strip()


_AGENT_CENTRIC_PAGE_AGENT = """
## 页面任务补充 v2

- 页面 CLI 消息与工作台按钮都是任务输入。需要后端代码事实时使用原生工具，需要页面状态或平台工件时使用 MCP 工具。
- 路由和机会推理先调用 prepare action 获得精确 context artifact，按规定 Schema 形成结果，再调用 run/save action 校验并回填。
- 大代码仓任务实行价值优先交付：先发布有证据的最小可用产物并声明未覆盖范围，再根据用户目标或新证据深入，不为穷举整仓延迟首次结果。
""".strip()


_AGENT_CENTRIC_PAGE_ANALYSIS = """
## 页面分析工件补充 v2

1. 硬件路由必须是 `prepare_hardware_semantic_route -> 按契约形成路由 -> run_hardware_semantic_route(context_artifact_id, agent_result)`。
2. 机会推理必须是 `prepare_context -> 按契约形成机会结果 -> run_strategy(context_artifact_id, agent_result)`。
3. prepare 负责确定性上下文，run/save 负责契约校验、持久化和可视化。
""".strip()


_ADAPTIVE_DEVICE = """
## 自适应回退补充 v1

- requested_unit 和 API family 是当前实验假设，不是永久锁定。真实 profile、编译、运行时 capability 或功能比较若否定当前 API，可先在同一单元内重选 API/template/profile；若否定的是单元语义，则调用页面路由 action 生成新路由，再回到机会推理。
- 不因缺少端到端收益证据拒绝最小功能实验；但必须把“核心可执行、功能等价、真实硬件参与、热点集成、端到端收益”分层报告。
- 只在重复调用无法产生新事实、工作站安全边界被触发或缺少用户才能提供的信息时停止。门禁应指出证据层级和恢复动作，不把待验证项伪装成永久禁止。
""".strip()


_LEGACY_FIXED_HARNESS_DEVICE = """
## 旧版固定 Harness 执行边界

本 profile 保留 v2.x 的固定 Harness 优先行为，用于回放旧记录和对照实验。它覆盖基础 Skill 第 6、7、9 条的执行路径：构建必须使用固定 `build_describe_profile` / `build_stage_source` / `build_create`，设备操作使用类型化 `device_*` action。不得把该旧 profile 的约束外推为 Kimi Agent 引擎的全局权限边界。
""".strip()


_ADAPTIVE_FULL_ANALYSIS = """
## 自适应候选与门禁补充 v1

- 先给出源码可支持的最小作用域候选，再校准整条路径的证据等级。设备能力、搬运、同步、功耗或 benchmark 缺失时，把它们列为 required_conditions / missing_evidence；除非它们直接否定语义或安全合同，否则不要删除核心候选。
- 当前主路由可以被本轮策略拒绝。若拒绝，明确输出“需要重新路由”的原因和建议比较的硬件类别；不要在没有对应 API context 的情况下直接宣称新 API 已匹配。
- recommendation 表示当前证据成熟度，不等同于“是否值得继续验证”。允许在 recommendation=evidence_required 时保留具体、窄作用域、可证伪的候选方向。
""".strip()


_ADAPTIVE_HARDWARE_STRATEGY = """
## 路由可修订补充 v1

- 主硬件目录是本轮检索入口而非硬约束。先精核该目录 API；若代码语义与全部候选均不相容，明确拒绝本目录并给出 `reroute_recommended` 的中文理由。真正切换仍由新路由 action 完成。
- 同一单元内某个 API 不相容时，不把整个单元一并否定；指出应重选的语义族、输入输出合同或 profile。反之，运行时事实否定整个单元可达性时，不要靠更换同类 API 掩盖。
- 端到端未知项用于校准置信度和设计验证，不得压过可见代码中明确的规则并行、像素/张量/PCM/码流语义核心。
""".strip()


_ADAPTIVE_ROUTE = """
## 可逆路由补充 v1

本路由是可修订假设。若输入带有上一轮失败原因，应重新比较本轮注册的全部平台与用户自建能力卡，不要机械复述旧选择；说明是同目录 API 重选更合适，还是需要更换硬件单元。后续新证据可以触发再次路由。
""".strip()


BLIND_REASONING_SKILL = """
# 独立盲推理基线 Skill v1.0.0

只使用本轮给出的用户问题、热点代码和目标平台画像，形成鸿蒙异构与并行优化的独立基线。

1. 不假设存在优化知识库、硬件 API 检索、静态规则路由、Trace 结论或设备探测；不得声称你看过这些内容。
2. 先识别最小计算核心、数据类型与形态、访存、依赖、控制流、任务粒度和外部边界，再判断算法、CPU 并行、SIMD 或异构方向。
3. 可以使用通用工程知识提出 NPU、GPU、DSP、媒体硬件或 CPU 并行候选，并尽量具体到 API/API family；所有具体接口和设备可用性都标为待验证假设。
4. 目标平台画像只是分析范围。不得把麒麟或鸿蒙字样当作某个 API、驱动或硬件已存在的证明。
5. 对每个方向写出适用条件、主要风险和最小验证方法；代码不足时明确未知项，但不要因为缺少端到端代码而忽略可见的局部计算核心。
6. 输出使用简体中文，只返回一个严格 JSON 对象，不使用 Markdown。
""".strip()


BLIND_COMPARISON_SKILL = """
# 盲推理与流程推理对照 Skill v1.0.0

比较冻结的盲推理结论与稍后提供的流程推理结果，不要为了迎合流程结果而改写历史判断。

1. 比较负载识别、候选硬件/API、CPU 并行、前提条件、反证、风险与验证计划；若输入含真实设备工件，另外区分编译、设备执行、功能等价、硬件参与和性能收益。
2. 区分“流程或真机新增了可审计证据”“流程只是增加了约束”“盲推理使用了常识但缺少来源”“双方都遗漏”。
3. 给出共识、关键分歧、各自优缺点、无依据或过度保守项，以及合并后的下一步建议。
4. verdict 只能是 blind_better、flow_better、complementary 或 inconclusive。该 verdict 是同模型自评，不是真值或准确率证明。
5. 输出使用简体中文，只返回一个严格 JSON 对象，不使用 Markdown。
""".strip()


_COMMON_NEW_SKILLS = {
    "blind-opportunity-analysis": {
        "version": "1.0.0",
        "prompt_slot": "blind_reasoning",
        "prompt": BLIND_REASONING_SKILL,
        "change_summary_zh": "新增与主流程隔离的代码与平台画像盲推理基线。",
    },
    "blind-flow-comparison": {
        "version": "1.0.0",
        "prompt_slot": "blind_comparison",
        "prompt": BLIND_COMPARISON_SKILL,
        "change_summary_zh": "对照冻结的盲推结果与正式流程结果并输出诊断。",
    },
}


_PROFILES: dict[str, dict[str, Any]] = {
    "stable-2026-08-13": {
        "profile_id": "stable-2026-08-13",
        "label_zh": "稳定版（原流程）",
        "description_zh": "保留引入盲推理和可逆路由之前的主流程 Skill 行为；新增盲推理仍可手动运行。",
        "created_at": "2026-08-13T00:00:00Z",
        "recommended": False,
        "skills": {
            "page-agent-control": {"version": "3.0.0", "prompt_slot": "page_agent", "addendum": ""},
            "page-analysis-artifact-orchestrator": {"version": "1.0.0", "prompt_slot": "page_analysis", "addendum": ""},
            "agent-native-device-experimentation": {"version": "2.8.0", "prompt_slot": "device_validation", "addendum": _LEGACY_FIXED_HARNESS_DEVICE},
            "full-optimization-analysis": {"version": "1.5.0", "prompt_slot": "full_analysis", "addendum": ""},
            "strategy-hardware-kb": {"version": "2.2.0", "prompt_slot": "strategy_hardware_kb", "addendum": ""},
            "hardware-semantic-routing-experiment": {"version": "2.4.0", "prompt_slot": "hardware_route", "addendum": ""},
            **_COMMON_NEW_SKILLS,
        },
    },
    "adaptive-2026-08-14": {
        "profile_id": "adaptive-2026-08-14",
        "label_zh": "自适应版（前置盲推）",
        "description_zh": "增加独立盲推理、流程结果对照、软门禁以及策略/真机阶段的重新路由与 API 重选。",
        "created_at": "2026-08-14T00:00:00Z",
        "recommended": False,
        "parent_profile_id": "stable-2026-08-13",
        "skills": {
            "page-agent-control": {"version": "3.1.0", "prompt_slot": "page_agent", "addendum": _ADAPTIVE_PAGE_AGENT},
            "page-analysis-artifact-orchestrator": {"version": "1.1.0", "prompt_slot": "page_analysis", "addendum": _ADAPTIVE_PAGE_ANALYSIS},
            "agent-native-device-experimentation": {"version": "2.9.0", "prompt_slot": "device_validation", "addendum": f"{_LEGACY_FIXED_HARNESS_DEVICE}\n\n{_ADAPTIVE_DEVICE}"},
            "full-optimization-analysis": {"version": "1.6.0", "prompt_slot": "full_analysis", "addendum": _ADAPTIVE_FULL_ANALYSIS},
            "strategy-hardware-kb": {"version": "2.3.0", "prompt_slot": "strategy_hardware_kb", "addendum": _ADAPTIVE_HARDWARE_STRATEGY},
            "hardware-semantic-routing-experiment": {"version": "2.5.0", "prompt_slot": "hardware_route", "addendum": _ADAPTIVE_ROUTE},
            **_COMMON_NEW_SKILLS,
        },
    },
    "adaptive-post-validation-2026-08-14": {
        "profile_id": "adaptive-post-validation-2026-08-14",
        "label_zh": "自适应版（后置盲推）",
        "description_zh": "先完成路由、机会推理和真机验证，再以隔离输入运行盲推基线并对照流程结果。",
        "created_at": "2026-08-14T04:00:00Z",
        "recommended": False,
        "parent_profile_id": "adaptive-2026-08-14",
        "skills": {
            "page-agent-control": {"version": "3.2.0", "prompt_slot": "page_agent", "addendum": _POST_VALIDATION_PAGE_AGENT},
            "page-analysis-artifact-orchestrator": {"version": "1.2.0", "prompt_slot": "page_analysis", "addendum": _POST_VALIDATION_PAGE_ANALYSIS},
            "agent-native-device-experimentation": {"version": "2.9.0", "prompt_slot": "device_validation", "addendum": f"{_LEGACY_FIXED_HARNESS_DEVICE}\n\n{_ADAPTIVE_DEVICE}"},
            "full-optimization-analysis": {"version": "1.6.0", "prompt_slot": "full_analysis", "addendum": _ADAPTIVE_FULL_ANALYSIS},
            "strategy-hardware-kb": {"version": "2.3.0", "prompt_slot": "strategy_hardware_kb", "addendum": _ADAPTIVE_HARDWARE_STRATEGY},
            "hardware-semantic-routing-experiment": {"version": "2.5.0", "prompt_slot": "hardware_route", "addendum": _ADAPTIVE_ROUTE},
            **_COMMON_NEW_SKILLS,
        },
    },
    "native-backend-2026-08-19": {
        "profile_id": "native-backend-2026-08-19",
        "label_zh": "原生后端执行",
        "description_zh": "Kimi 直接操作后端代码、文件、Shell、编译器和同机 HDC；MCP 仅承载页面、平台工件和 Windows HDC 跨边界能力。",
        "created_at": "2026-08-19T00:00:00Z",
        "recommended": False,
        "parent_profile_id": "adaptive-post-validation-2026-08-14",
        "skills": {
            "page-agent-control": {"version": "3.2.0", "prompt_slot": "page_agent", "addendum": _POST_VALIDATION_PAGE_AGENT},
            "page-analysis-artifact-orchestrator": {"version": "1.2.0", "prompt_slot": "page_analysis", "addendum": _POST_VALIDATION_PAGE_ANALYSIS},
            "agent-native-device-experimentation": {"version": "3.0.0", "prompt_slot": "device_validation", "addendum": _ADAPTIVE_DEVICE},
            "full-optimization-analysis": {"version": "1.6.0", "prompt_slot": "full_analysis", "addendum": _ADAPTIVE_FULL_ANALYSIS},
            "strategy-hardware-kb": {"version": "2.3.0", "prompt_slot": "strategy_hardware_kb", "addendum": _ADAPTIVE_HARDWARE_STRATEGY},
            "hardware-semantic-routing-experiment": {"version": "2.5.0", "prompt_slot": "hardware_route", "addendum": _ADAPTIVE_ROUTE},
            **_COMMON_NEW_SKILLS,
        },
    },
    "agent-centric-2026-08-19": {
        "profile_id": "agent-centric-2026-08-19",
        "label_zh": "Agent-Centric 完整版",
        "description_zh": "页面提交意图与投影工件，分析链覆盖路由、机会推理、流程分析和真机迭代。",
        "created_at": "2026-08-19T06:00:00Z",
        "recommended": False,
        "parent_profile_id": "native-backend-2026-08-19",
        "skills": {
            "page-agent-control": {"version": "4.0.0", "prompt_slot": "page_agent", "addendum": f"{_POST_VALIDATION_PAGE_AGENT}\n\n{_AGENT_CENTRIC_PAGE_AGENT}"},
            "page-analysis-artifact-orchestrator": {"version": "2.0.0", "prompt_slot": "page_analysis", "addendum": f"{_POST_VALIDATION_PAGE_ANALYSIS}\n\n{_AGENT_CENTRIC_PAGE_ANALYSIS}"},
            "agent-native-device-experimentation": {"version": "3.0.0", "prompt_slot": "device_validation", "addendum": _ADAPTIVE_DEVICE},
            "full-optimization-analysis": {"version": "1.6.0", "prompt_slot": "full_analysis", "addendum": _ADAPTIVE_FULL_ANALYSIS},
            "strategy-hardware-kb": {"version": "2.3.0", "prompt_slot": "strategy_hardware_kb", "addendum": _ADAPTIVE_HARDWARE_STRATEGY},
            "hardware-semantic-routing-experiment": {"version": "2.5.0", "prompt_slot": "hardware_route", "addendum": _ADAPTIVE_ROUTE},
            **_COMMON_NEW_SKILLS,
        },
    },
    "agent-centric-lean-2026-08-19": {
        **_load_source_profile("agent-centric-lean-2026-08-19.json"),
        "recommended": False,
    },
    "agent-centric-lean-mermaid-2026-08-19": {
        **_load_source_profile("agent-centric-lean-mermaid-2026-08-19.json"),
        "recommended": False,
    },
    "agent-centric-fast-2026-08-20": {
        **_load_source_profile("agent-centric-fast-2026-08-20.json"),
        "recommended": False,
    },
    "agent-centric-page-2026-08-20": _load_source_profile("agent-centric-page-2026-08-20.json"),
    "agent-centric-page-fast-flow-2026-08-20": _load_source_profile("agent-centric-page-fast-flow-2026-08-20.json"),
    "agent-centric-page-three-stage-flow-2026-08-20": _load_source_profile("agent-centric-page-three-stage-flow-2026-08-20.json"),
    "agent-centric-page-adaptive-flow-2026-08-20": {
        **_load_source_profile("agent-centric-page-adaptive-flow-2026-08-20.json"),
        "recommended": False,
    },
    "agent-centric-page-semantic-code-2026-08-28": {
        **_load_source_profile("agent-centric-page-semantic-code-2026-08-28.json"),
        "recommended": False,
    },
    "agent-centric-page-semantic-code-2026-08-31": {
        **_load_source_profile("agent-centric-page-semantic-code-2026-08-31.json"),
        "recommended": False,
    },
    "agent-centric-page-device-preparation-2026-10-22": {
        **_load_source_profile("agent-centric-page-device-preparation-2026-10-22.json"),
        "recommended": False,
    },
    DEFAULT_SKILL_PROFILE_ID: _load_source_profile("agent-centric-page-device-selection-2026-10-26.json"),
}


def user_skill_store() -> UserSkillStore:
    return _USER_SKILL_STORE


def set_user_skill_store_for_testing(store: UserSkillStore) -> UserSkillStore:
    """Swap the mutable store for an isolated unit-test instance."""

    global _USER_SKILL_STORE
    previous = _USER_SKILL_STORE
    _USER_SKILL_STORE = store
    return previous


def _materialize_profile(profile: dict[str, Any], *, include_user: bool = True) -> dict[str, Any]:
    value = deepcopy(profile)
    skills = value.get("skills") if isinstance(value.get("skills"), dict) else {}
    for skill_id, entry in _global_server_skills().items():
        skills.setdefault(skill_id, deepcopy(entry))
    for skill_id, skill in skills.items():
        if not isinstance(skill, dict):
            continue
        prompt_material = str(skill.get("prompt") or skill.get("addendum") or "")
        skill["prompt_mode"] = str(skill.get("prompt_mode") or "append")
        skill["origin"] = "server"
        skill["content_chars"] = len(prompt_material)
        skill["content_sha256"] = sha256_json({
            "skill_id": skill_id,
            "version": skill.get("version"),
            "prompt_slot": skill.get("prompt_slot"),
            "prompt_mode": skill.get("prompt_mode"),
            "prompt_material": prompt_material,
        })
    user_skill_revision = 0
    if include_user:
        user_skill_revision, active_user_skills = _USER_SKILL_STORE.active_snapshot()
        for skill_id, user_version in active_user_skills.items():
            server_entry = skills.get(skill_id) if isinstance(skills.get(skill_id), dict) else {}
            prompt_slot = str(user_version.get("prompt_slot") or server_entry.get("prompt_slot") or f"custom:{skill_id}")
            prompt = str(user_version.get("content") or "").strip()
            skills[skill_id] = {
                **deepcopy(server_entry),
                "version": str(user_version.get("version") or "user"),
                "prompt_slot": prompt_slot,
                "prompt_mode": "replace",
                "prompt": prompt,
                "source": f"user-skill-library/{skill_id}/{user_version.get('version_id')}",
                "origin": "user",
                "user_version_id": str(user_version.get("version_id") or ""),
                "created_at": user_version.get("created_at"),
                "created_by": user_version.get("created_by"),
                "label_zh": user_version.get("label_zh"),
                "description_zh": user_version.get("description_zh"),
                "change_summary_zh": user_version.get("change_summary_zh"),
                "based_on": deepcopy(user_version.get("based_on") if isinstance(user_version.get("based_on"), dict) else {}),
                "server_version": str(server_entry.get("version") or "") or None,
                "content_chars": len(prompt),
                "content_sha256": str(user_version.get("content_sha256") or sha256_json({"skill_id": skill_id, "content": prompt})),
            }
    value["user_skill_revision"] = user_skill_revision
    value["skill_resolution_order"] = ["user_active_version", "server_profile"]
    value["profile_sha256"] = sha256_json({
        "profile_id": value.get("profile_id"),
        "skills": {
            key: {
                "version": item.get("version"),
                "content_sha256": item.get("content_sha256"),
                "origin": item.get("origin"),
                "user_version_id": item.get("user_version_id"),
            }
            for key, item in sorted(skills.items())
            if isinstance(item, dict)
        },
    })
    return value


def skill_version_library() -> dict[str, Any]:
    user_catalog = _USER_SKILL_STORE.catalog()
    return {
        "schema_version": "skill_version_library/v1",
        "default_profile_id": DEFAULT_SKILL_PROFILE_ID,
        "profiles": [_materialize_profile(profile) for profile in _PROFILES.values()],
        "user_skill_library": {
            "revision": user_catalog["revision"],
            "updated_at": user_catalog["updated_at"],
            "skill_count": user_catalog["skill_count"],
            "resolution_order": ["user_active_version", "server_profile"],
        },
        "selection_scope_zh": "平台 profile 提供只读基线；激活的用户自编辑 Skill 优先热加载。版本切换只影响后续推理，不改写历史工件。",
    }


def resolve_skill_profile(profile_id: str | None) -> dict[str, Any]:
    selected = profile_id or DEFAULT_SKILL_PROFILE_ID
    profile = _PROFILES.get(selected)
    if profile is None:
        raise PlatformError(
            "SKILL_PROFILE_UNKNOWN",
            "所选 Skill 版本配置不存在。",
            status_code=422,
            details={"profile_id": selected, "available_profile_ids": sorted(_PROFILES)},
        )
    return _materialize_profile(profile)


def resolve_server_skill_profile(profile_id: str | None) -> dict[str, Any]:
    selected = profile_id or DEFAULT_SKILL_PROFILE_ID
    profile = _PROFILES.get(selected)
    if profile is None:
        raise PlatformError(
            "SKILL_PROFILE_UNKNOWN",
            "所选 Skill 版本配置不存在。",
            status_code=422,
            details={"profile_id": selected, "available_profile_ids": sorted(_PROFILES)},
        )
    return _materialize_profile(profile, include_user=False)


def _server_management_skills(profile_id: str | None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return profile prompts plus deterministic server Skill contracts."""

    profile = resolve_server_skill_profile(profile_id)
    skills = deepcopy(profile.get("skills") if isinstance(profile.get("skills"), dict) else {})
    # Lazy import keeps the source-controlled prompt registry independent from
    # the deterministic action/manifest registry during module initialization.
    from .skills import skill_catalog

    for manifest in skill_catalog():
        skill_id = str(manifest.get("skill_id") or "").strip()
        if not skill_id or skill_id in skills:
            continue
        required_evidence = manifest.get("required_evidence") if isinstance(manifest.get("required_evidence"), list) else []
        quality_gates = manifest.get("quality_gates") if isinstance(manifest.get("quality_gates"), list) else []
        content = "\n".join([
            f"# {skill_id}",
            "",
            f"- 执行器：{manifest.get('executor') or '平台确定性执行'}",
            f"- 能力边界：{manifest.get('claim_boundary') or '遵守登记的输入、输出与证据合同。'}",
            *(["- 必需证据：" + "；".join(str(item) for item in required_evidence[:8])] if required_evidence else []),
            *[
                f"- 门禁：{gate.get('requirement')}"
                for gate in quality_gates[:6]
                if isinstance(gate, dict) and gate.get("requirement")
            ],
        ]).strip()
        skills[skill_id] = {
            "version": str(manifest.get("version") or "1.0.0"),
            "prompt_slot": f"custom:{skill_id}",
            "prompt_mode": "replace",
            "prompt": content,
            "source": "server-skill-manifest",
            "origin": "server",
            "content_chars": len(content),
            "content_sha256": sha256_json({"skill_id": skill_id, "version": manifest.get("version"), "content": content}),
            "server_contract_only": True,
        }
    return profile, skills


def skill_management_catalog(profile_id: str | None) -> dict[str, Any]:
    server_profile, server_skills = _server_management_skills(profile_id)
    effective_profile = resolve_skill_profile(profile_id)
    user_catalog = _USER_SKILL_STORE.catalog()
    user_records = {
        str(item.get("skill_id")): item
        for item in user_catalog.get("skills", [])
        if isinstance(item, dict) and item.get("skill_id")
    }
    effective_skills = effective_profile.get("skills") if isinstance(effective_profile.get("skills"), dict) else {}
    entries: list[dict[str, Any]] = []
    for skill_id in sorted(set(server_skills) | set(user_records)):
        server_entry = server_skills.get(skill_id) if isinstance(server_skills.get(skill_id), dict) else {}
        effective_entry = effective_skills.get(skill_id) if isinstance(effective_skills.get(skill_id), dict) else server_entry
        user_record = user_records.get(skill_id) if isinstance(user_records.get(skill_id), dict) else {}
        user_versions = user_record.get("versions") if isinstance(user_record.get("versions"), list) else []
        versions: list[dict[str, Any]] = []
        if server_entry:
            versions.append({
                "version_id": "server",
                "version": server_entry.get("version"),
                "origin": "server",
                "content_sha256": server_entry.get("content_sha256"),
                "content_chars": server_entry.get("content_chars"),
                "active": effective_entry.get("origin") != "user",
                "created_at": server_profile.get("created_at"),
            })
        versions.extend(user_versions)
        entries.append({
            "skill_id": skill_id,
            "label_zh": effective_entry.get("label_zh") or user_record.get("label_zh") or skill_id,
            "description_zh": effective_entry.get("description_zh") or user_record.get("description_zh") or "",
            "prompt_slot": effective_entry.get("prompt_slot") or server_entry.get("prompt_slot") or f"custom:{skill_id}",
            "effective_origin": effective_entry.get("origin") or ("server" if server_entry else "inactive_user"),
            "effective_version": effective_entry.get("version") or None,
            "effective_version_id": effective_entry.get("user_version_id") or ("server" if server_entry else None),
            "active_user_version_id": user_record.get("active_version_id"),
            "server_version": server_entry.get("version") or None,
            "has_server_version": bool(server_entry),
            "user_version_count": len(user_versions),
            "versions": versions,
        })
    return {
        "schema_version": "skill_management_library/v1",
        "profile_id": effective_profile["profile_id"],
        "profile_sha256": effective_profile["profile_sha256"],
        "user_skill_revision": user_catalog["revision"],
        "resolution_order": ["user_active_version", "server_profile"],
        "skills": entries,
    }


def skill_management_detail(
    skill_id: str,
    *,
    profile_id: str | None,
    version_id: str | None = None,
) -> dict[str, Any]:
    server_profile, server_skills = _server_management_skills(profile_id)
    effective_profile = resolve_skill_profile(profile_id)
    effective_skills = effective_profile.get("skills") if isinstance(effective_profile.get("skills"), dict) else {}
    server_entry = server_skills.get(skill_id) if isinstance(server_skills.get(skill_id), dict) else None
    selected_version_id = (version_id or "").strip()
    if selected_version_id and selected_version_id != "server":
        selected = _USER_SKILL_STORE.get_version(skill_id, selected_version_id)
        return {
            "schema_version": "skill_management_detail/v1",
            "skill": selected,
            "effective": selected_version_id == str((effective_skills.get(skill_id) or {}).get("user_version_id") or ""),
            "server_baseline": {
                "version": server_entry.get("version"),
                "content_sha256": server_entry.get("content_sha256"),
            } if server_entry else None,
        }
    if selected_version_id == "server":
        if server_entry is None:
            raise PlatformError("SERVER_SKILL_NOT_FOUND", "平台 Skill 仓中不存在该 Skill。", status_code=404, details={"skill_id": skill_id})
        selected = deepcopy(server_entry)
        selected["skill_id"] = skill_id
        selected["content"] = str(selected.get("prompt") or selected.get("addendum") or "")
        return {"schema_version": "skill_management_detail/v1", "skill": selected, "effective": effective_skills.get(skill_id, {}).get("origin") != "user", "server_baseline": None}
    effective = effective_skills.get(skill_id) if isinstance(effective_skills.get(skill_id), dict) else server_entry
    if effective is None:
        raise PlatformError("SKILL_NOT_FOUND", "Skill 不存在。", status_code=404, details={"skill_id": skill_id})
    selected = deepcopy(effective)
    selected["skill_id"] = skill_id
    selected["content"] = str(selected.get("prompt") or selected.get("addendum") or "")
    return {
        "schema_version": "skill_management_detail/v1",
        "skill": selected,
        "effective": True,
        "server_baseline": {
            "version": server_entry.get("version"),
            "content_sha256": server_entry.get("content_sha256"),
        } if server_entry and effective.get("origin") == "user" else None,
    }


def save_user_skill_version(
    *,
    skill_id: str,
    label_zh: str,
    description_zh: str,
    content: str,
    profile_id: str | None,
    prompt_slot: str = "",
    change_summary_zh: str = "",
    created_by: str = "web_ui",
) -> dict[str, Any]:
    server_profile = resolve_server_skill_profile(profile_id)
    effective_profile = resolve_skill_profile(profile_id)
    server_entry = (server_profile.get("skills") or {}).get(skill_id)
    effective_entry = (effective_profile.get("skills") or {}).get(skill_id)
    if isinstance(server_entry, dict):
        resolved_slot = str(server_entry.get("prompt_slot") or "")
        if prompt_slot and prompt_slot != resolved_slot:
            raise PlatformError(
                "USER_SKILL_PROMPT_SLOT_CONFLICT",
                "已有平台 Skill 的 prompt slot 不能由用户版本改写。",
                status_code=422,
                details={"skill_id": skill_id, "expected_prompt_slot": resolved_slot},
            )
    else:
        resolved_slot = prompt_slot or str((effective_entry or {}).get("prompt_slot") or f"custom:{skill_id}")
    based_on = {
        "origin": str((effective_entry or {}).get("origin") or "server"),
        "profile_id": str(effective_profile.get("profile_id") or ""),
        "version": str((effective_entry or {}).get("version") or ""),
        "version_id": str((effective_entry or {}).get("user_version_id") or ""),
        "content_sha256": str((effective_entry or {}).get("content_sha256") or ""),
    }
    version = _USER_SKILL_STORE.save_version(
        skill_id=skill_id,
        label_zh=label_zh,
        description_zh=description_zh,
        content=content,
        prompt_slot=resolved_slot,
        change_summary_zh=change_summary_zh,
        created_by=created_by,
        based_on=based_on,
        activate=True,
    )
    return {
        "status": "succeeded",
        "resolution": "user_active_version",
        "version": version,
        "effective_profile": resolve_skill_profile(profile_id),
    }


def activate_user_skill_version(skill_id: str, version_id: str | None, *, profile_id: str | None) -> dict[str, Any]:
    _server_profile, server_skills = _server_management_skills(profile_id)
    if not version_id and skill_id not in server_skills:
        # A user-only Skill can be deactivated; it remains in history and is no
        # longer discoverable until a user version is reactivated.
        pass
    activation = _USER_SKILL_STORE.activate(skill_id, version_id)
    return {
        "status": "succeeded",
        "activation": activation,
        "effective_profile": resolve_skill_profile(profile_id),
    }


def _skill_entry(profile_id: str | None, prompt_slot: str) -> dict[str, Any] | None:
    profile = resolve_skill_profile(profile_id)
    skills = profile.get("skills") if isinstance(profile.get("skills"), dict) else {}
    for skill in skills.values():
        if isinstance(skill, dict) and skill.get("prompt_slot") == prompt_slot:
            return skill
    return None


def skill_prompt(profile_id: str | None, prompt_slot: str) -> str:
    skill = _skill_entry(profile_id, prompt_slot)
    return str((skill or {}).get("prompt") or (skill or {}).get("addendum") or "").strip()


def apply_skill_prompt(base_prompt: str, profile_id: str | None, prompt_slot: str) -> str:
    skill = _skill_entry(profile_id, prompt_slot)
    material = str((skill or {}).get("prompt") or (skill or {}).get("addendum") or "").strip()
    if not material:
        return base_prompt
    if str((skill or {}).get("prompt_mode") or "append") == "replace":
        return material
    return f"{base_prompt.rstrip()}\n\n{material}"


def append_skill_overlay(base_prompt: str, profile_id: str | None, prompt_slot: str) -> str:
    """Backward-compatible name for applying a versioned Skill material."""

    return apply_skill_prompt(base_prompt, profile_id, prompt_slot)
