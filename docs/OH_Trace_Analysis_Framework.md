# OpenHarmony Trace 分析整体框架

本文档汇总 `oh-trace-analysis-clean` 与 `Xicen` 两个项目的协作方式，解释从源码到 trace 分析、再到 stage 分解的完整链路。

---

## 1. 通过大模型解读代码仓生成 Mermaid 流程图

### 1.1 当前状态

目前两个仓库里**没有全自动的 LLM → Mermaid 生成脚本**。Mermaid 文件当前是人工编写、或在大模型辅助下编写的：

| 文件 | 路径 | 说明 |
|---|---|---|
| `ai_agent_flow.mmd` | `/path/to/oh-trace-analysis/ai_agent_flow.mmd` | AI Agent 执行流程序列图，Xicen 配置的主要来源。 |
| `camera_config_flow_from_code.mmd` | `/path/to/oh-trace-analysis/camera_config_flow_from_code.mmd` | Camera 配置流程示例。 |

### 1.2 推荐做法

把目标模块的源码（或关键函数调用链）提供给大模型，提示词模板如下：

```text
你是一位 OpenHarmony 系统专家。请阅读下面这段代码，识别其中涉及的主要子系统/模块，
以及它们之间的调用/事件/数据流转关系，然后输出一个 Mermaid sequenceDiagram。

要求：
- 用 participant XXX as "Human Name" 声明每个角色。
- 用 ->> 表示同步调用，-->> 表示返回/事件。
- 把关键阶段用 rect rgb(...) 框起来，并给出阶段名。
- 尽量与已有的 HITRACE 桩名对应。

代码：
{paste source}
```

大模型输出保存为 `.mmd` 后，下游的 `mermaid_to_phase_config.py` 可以消费它。

### 1.3 Mermaid 中的配置扩展

为了便于后续生成 Xicen 配置，`.mmd` 里支持两类注释：

```mermaid
%% xicen-param: touch_lookback_ns 1000000000
%% trace: touch -> H:touchEventDispatch
```

- `%% xicen-param: key value`：覆盖输出 config 里的参数。
- `%% trace: label -> pattern`：显式指定某个阶段对应的事件名模式。

---

## 2. 扫描代码仓生成 HITRACE 桩模块映射表（JSON）

### 2.1 主扫描工具

**文件：** `/path/to/oh-trace-analysis/scripts/discover_trace_patterns.py`

该脚本递归扫描 C/C++/Java/JS/TS/ets 源码，识别常见的 trace 埋点：

- `HITRACE_METER_NAME(..., "name")`
- `HITRACE_METER("name")`
- `StartTrace("name")` / `FinishTrace("name")`
- `"H:<name>"` 字符串字面量
- 可选 `--include-logs` 时也会扫描 `HILOG_*` / `ACE_*` 等日志宏

### 2.2 使用方法

```bash
cd /path/to/oh-trace-analysis
source venv/bin/activate

# 基础扫描
python3 scripts/discover_trace_patterns.py /path/to/source_root \
    -o config/draft_trace_event_map.json

# 带模块名映射
python3 scripts/discover_trace_patterns.py /path/to/source_root \
    --module-map '{"arkui_ace_engine": "ArkUI", "camera": "Camera"}' \
    -o config/draft_trace_event_map.json
```

### 2.3 输出 JSON 结构

```json
{
  "domain": "source_root_name",
  "description": "Draft trace event map discovered from source code.",
  "participants": {
    "ArkUI": {
      "process_names": [],
      "note": "process names must be verified against trace"
    }
  },
  "events": {
    "touch_event_dispatch": {
      "modules": ["ArkUI"],
      "patterns": ["H:touchEventDispatch", "touchEventDispatch"],
      "sources": ["foundation/arkui/ace_engine/.../xxx.cpp"]
    }
  }
}
```

- `events.<key>.modules`：这个 trace 事件可能属于哪些子系统/模块。
- `events.<key>.patterns`：在真实 trace 中可能匹配到的名字列表。
- `events.<key>.sources`：源码中出现的位置。

### 2.4 限制

- 只能扫到本地有的源码。像 HiAI / MSDP / AIDispatch 等 AI 相关模块如果本地没有源码，事件名仍需人工整理。
- 进程名 (`process_names`) 无法从源码直接得到，需要在真实 trace 中验证后回填。

### 2.5 HITRACE tag 位提取

**文件：** `/path/to/oh-trace-analysis/scripts/extract_hitrace_tags.py`

解析 `hitrace_meter.h` 里的 `HITRACE_TAG_*` 位定义，生成：

```bash
python scripts/extract_hitrace_tags.py [path/to/hitrace_meter.h]
```

输出：`oh_trace_analysis/hitrace_tag_map.py`，包含 `TAG_INDEX_TO_NAME`、`OPTION_TAG_NAMES`、`TAG_INDEX_TO_SUBSYSTEM`。

---

## 3. Xicen 依赖的 config 文件生成工具

### 3.1 工具位置

**文件：** `/path/to/oh-trace-analysis/scripts/mermaid_to_phase_config.py`

它把 Mermaid 流程图和 HITRACE 模块映射表合并，生成 Xicen 可消费的 JSON/YAML 配置。

### 3.2 生成 legacy Xicen 配置

```bash
cd /path/to/oh-trace-analysis
python3 scripts/mermaid_to_phase_config.py ai_agent_flow.mmd \
    --format xicen \
    --trace-map config/ai_agent_trace_event_map.json \
    -o ai_agent_flow.xicen.json
```

### 3.3 生成原理

1. **解析 Mermaid**
   - 提取 `participant ... as ...`。
   - 提取消息 `A->>B: text`、`rect`/`loop`/`alt`/`opt`/`par` 块。
2. **阶段识别（关键字匹配）**
   - `touch` → `agent_stage:input_event_dispatch`
   - `aidataservice` → `agent_stage:aidataservice_startup`
   - `msdp` → `agent_stage:msdp_collection`
   - `classifier` / `classification` → `agent_stage:text_classification` / `image_classification`
   - `intent` → `agent_stage:intent_summary`
   - `tokenizer` → `agent_stage:tokenizer`
   - `init weights` / `initweights` → `agent_stage:inference_initialization`
   - `prefill` / `decode` → `agent_stage:prefill` / `decode`
3. **合并 trace map**
   - 如果 `--trace-map` 提供了 `events`，就用其中的 `patterns` 填充 `patterns` 字段。
   - 否则使用内置默认值。
4. **输出 schema**

```json
{
  "name": "AI Agent Flow",
  "source": "ai_agent_flow.mmd",
  "participants": { "A": { "label": "A", "process_pattern": "A" } },
  "parameters": { "touch_lookback_ns": 1000000000, ... },
  "patterns": { "touch": [...], "classifier": {...}, "process": {...} },
  "stages": [
    { "id": "agent_stage:...", "name": "...", "resolver": "...", "order": N }
  ]
}
```

### 3.4 生成 chain-policy 配置

`mermaid_to_phase_config.py` 当前生成的是 legacy 风格的 `ai_agent_flow.json`（即 `stages` + `resolver`）。

`ai_agent_flow_chain.json` 在此基础上增加了 `chain_policy`（`core` + `links`），由 chain engine 消费。这个扩展目前需要人工把 legacy config 和 link 规则组合起来，或后续扩展脚本直接输出。

### 3.5 用户可自定义

生成的 `/path/to/Xicen/code/xicen/configs/ai_agent_flow_chain.json` 是最终用户可编辑的：

- 改 `parameters` 里的阈值。
- 改 `patterns` 里的事件名、进程名、线程角色。
- 改 `chain_policy.links` 里的 link 模式、`search_from` fallback 列表。

改完后直接运行 Xicen 即可，无需再改 Python。

---

## 4. 当前 trace 泛化通用情况

### 4.1 已经 config 化的部分

| 能力 | 配置位置 | 说明 |
|---|---|---|
| 事件名识别 | `patterns.runv3`、`patterns.touch`、... | RunV3、binder reply、Running 状态等都可通过配置改。 |
| 进程名识别 | `patterns.process.{app,vassistant,aidataservice,msdp,handoff,aiservice,hiai}` | 子串匹配，支持别名。 |
| 线程角色 | `patterns.thread_roles.{main_thread,model_thread}` | 模型线程/主线程的进程可配置。 |
| 数值阈值 | `parameters.*` | lookback、gap、ratio、max hops 等。 |
| link 策略 | `chain_policy.links[].mode` | event / callstack_parent / wakeup_chain / isolated_keypoint / msdp_path / aidataservice_path。 |
| source fallback | `chain_policy.links[].search_from` 支持数组 | 一个 stage 可配置多个备选 source。 |
| 核心 anchor | `chain_policy.core.pattern_key` / `process_key` | 核心 stage 可用任意事件+进程组合。 |

### 4.2 仍未完全泛化的部分

| 能力 | 状态 | 说明 |
|---|---|---|
| MSDP unknown process 推断 | ⚠️ 仍在代码里 | `_is_inferred_msdp_source`：当上游进程名为 unknown 且下游是 swing_service 时，把上游当成 MSDP。 |
| aidataservice cold-start fallback | ⚠️ 仍在代码里 | wakeup chain 找不到 aidataservice 时，回退到 aidataservice 线程上的 activity cluster。 |
| 数值阈值 | ⚠️ 可配置但默认写死 | 瓶颈严重等级、合成 relation confidence 等阈值未暴露。 |
| `running_state` 全链路传递 | ⚠️ 部分函数未传到底 | 虽然 `patterns.running_state` 已声明，但很多内部工具函数仍用默认 `"running"。` |

### 4.3 不同场景怎么调整

| 场景 | 调整方式 |
|---|---|
| 模型进程不是 `hiai` | 改 `patterns.thread_roles.model_thread.process_key` 和 `chain_policy.core.process_key`。 |
| 核心 anchor 不是 RunV3 | 新增 `patterns.my_anchor`，并在 `chain_policy.core.pattern_key` 里引用。 |
| 不相信 MSDP 推断 | 把 MSDP link 改成 `wakeup_chain` + `stop_process_key: msdp`，去掉 `infer_handoff_process_key`。 |
| MSDP 缺失时仍想找回 aidataservice | 保留 `agent_stage:aidataservice_startup` 的 `search_from` 数组 fallback。 |

---

## 5. Hotspots Function 列表

### 5.1 生成逻辑

**主入口：** `/path/to/oh-trace-analysis/oh_trace_analysis/analysis/analyzer.py`

关键函数：

- `_hotspots(df)`：按 `(module, function)` 总耗时排名。
- `_subsystem_hotspots(df)`：按 `(subsystem, function)` 总耗时排名。
- `_self_time_hotspots(df)`：按 `(module, function)` self 耗时排名。

self 耗时计算：

```python
self_dur_ms = dur_ms - sum(child.dur_ms for child in direct_children)
```

### 5.2 使用方式

```bash
cd /path/to/oh-trace-analysis
source venv/bin/activate
python -m oh_trace_analysis /path/to/hiprofiler.db -o output
```

输出在 `output/report.json` 中：

```json
{
  "rank": 1,
  "module": "other",
  "function": "APP_COMPONENT_LOAD",
  "total_duration_ms": 1602.092,
  "call_count": 1,
  "avg_duration_ms": 1602.092,
  "max_duration_ms": 1602.092,
  "min_duration_ms": 1602.092,
  "pct": 19.936
}
```

self-time hotspots 的字段换成 `self_duration_ms`。

### 5.3 数量限制

`oh_trace_analysis/analysis/config.py` 里的 `hotspot_limit` 默认是 **100**。

### 5.4 可视化

`oh_trace_analysis/visualization/timeline_visualizer.py` 会渲染 `hotspot_self_timeline.svg`，展示 top hotspots 随时间的 self-time 分布。

---

## 6. DAG 图生成逻辑

### 6.1 整体流程

```
hiprofiler DB
    ↓
DBReader → callstack / binder / wakeup / sched_slice / thread_process DataFrames
    ↓
NameParser + ModuleClassifier → 每条 callstack 的 module / subsystem
    ↓
RelationGraphBuilder → binder edges + wakeup edges
    ↓
TraceAnalyzer
    ├─ 统计 module/subsystem 耗时
    ├─ 构建 module_dag / subsystem_dag（总耗时）
    ├─ 构建 module_dag_self / subsystem_dag_self（self 耗时）
    ├─ hotspots 列表
    └─ call chains / process / thread summaries
    ↓
ReportVisualizer → module_dag.svg / subsystem_dag.svg
```

### 6.2 关键文件

| 文件 | 作用 |
|---|---|
| `oh_trace_analysis/inference/name_parser.py` | 从 `callstack.name` 解析函数名、原始模块、category、tag bits、subsystem。 |
| `oh_trace_analysis/inference/module_classifier.py` | 用 `config/module_rules.yaml` 里的规则推断 module；未知时回退到 `primary_subsystem`。 |
| `oh_trace_analysis/inference/relation_builder.py` | 构建 binder transaction → binder reply 边，以及 wakeup → running itid 边。 |
| `oh_trace_analysis/analysis/analyzer.py` | 计算 self time、构建 DAG、生成 hotspots。 |
| `oh_trace_analysis/visualization/visualizer.py` | 用 graphviz 渲染 DAG SVG。 |
| `oh_trace_analysis/visualization/chain_dag_visualizer.py` | 渲染 chain-level 交互 DAG。 |
| `oh_trace_analysis/visualization/focus_dag_visualizer.py` | 针对种子模块生成聚焦子图。 |

### 6.3 DAG 边怎么来

**从 callstack parent_id：**

`analyzer.py::_build_dag_from_callstack` 遍历 `callstack` 表，对每条记录：

1. 找到 `parent_id` 对应的父记录。
2. 父模块 → 子模块 建立一条边。
3. 累计 `call_count` 和 `total_duration_ms`。

`parent_id == 4294967295`（uint32 max）表示根节点，不参与建边。

**从跨进程关系：**

- **Binder 边**：通过 `binder transaction` 的 args key `26` 找到目标 slice id，连到对应的 `binder reply`。
- **Wakeup 边**：`sched_wakeup` 事件映射到当前正在运行的 `itid`，建立跨进程调用边。

### 6.4 节点过滤

- 排除 `other`、`unknown`、`swapper` 等模块。
- 边按 `call_count` 和 `total_duration_ms` 排序，最多保留 `dag_max_edges = 1000` 条。
- 即使某个模块只有关系边、没有耗时，也会作为 endpoint 保留，保证跨进程可见性。

### 6.5 可视化规则

- 节点大小 ∝ 耗时。
- 边粗细 ∝ 调用次数。
- 输出文件：
  - `module_dag.svg` / `module_dag_self.svg`
  - `subsystem_dag.svg` / `subsystem_dag_self.svg`
  - `chain_dag.svg`（链级交互图）

### 6.6 生成命令

```bash
cd /path/to/oh-trace-analysis
source venv/bin/activate
python -m oh_trace_analysis /path/to/hiprofiler.db -o output
```

输出目录里会包含 `report.json` 和所有 SVG。

---

## 7. 文件依赖总图

```
OpenHarmony 源码
    ├── discover_trace_patterns.py ──► config/ai_agent_trace_event_map.json
    │                                   (HITRACE 桩模块映射表)
    │
    ├── 大模型辅助编写 ──► ai_agent_flow.mmd
    │                       (Mermaid 流程图)
    │
    └── mermaid_to_phase_config.py
            ├── ai_agent_flow.mmd
            └── config/ai_agent_trace_event_map.json
                    ▼
            ai_agent_flow.xicen.json
                    ▼
            人工扩展 chain_policy
                    ▼
        Xicen/code/xicen/configs/ai_agent_flow_chain.json
                    ▼
        Xicen agent_flow_chain.py (chain engine)

hiprofiler DB
    └── oh_trace_analysis CLI
            ├── analyzer.py → hotspots
            ├── analyzer.py → module_dag / subsystem_dag
            ├── relation_builder.py → binder/wakeup edges
            └── visualizer.py → SVG
```

---

## 8. 总结

- **Mermaid**：当前人工/LLM 辅助编写，未来可扩展为自动 LLM 生成。
- **HITRACE 映射**：`discover_trace_patterns.py` 扫描源码，输出 JSON；AI 模块需人工补充。
- **Xicen 配置**：`mermaid_to_phase_config.py` 把 Mermaid + trace map 转成 config；`ai_agent_flow_chain.json` 是最终可用户自定义的声明式配置。
- **泛化**：事件名、进程名、线程角色、link 策略、fallback source 都已 config 化；MSDP 推断和 aidataservice fallback 仍是代码级经验。
- **Hotspots**：基于 module/function 总耗时和 self 耗时排名。
- **DAG**：从 callstack `parent_id` 和 binder/wakeup 关系构建 module/subsystem 依赖图。
