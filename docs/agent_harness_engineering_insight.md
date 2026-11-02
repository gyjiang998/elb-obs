# Agent Harness 工程洞察与演进建议

> 审阅快照：`436687c5862b5d849189ca9da80a97052a97105f`（2026-08-10）
> 2026-08-18 增补：纳入自建 API 合同发现 v2 的 Harness 分层与回归结论；其余量化盘点仍以原审阅快照为准。
> 审阅范围：Agent Runtime、Kimi Agent Engine、MCP/Action、页面动作、Build Worker、Device Gateway、可观测性、Skill/KB/模板、评测与上下文工程。
> 结论性质：这是工程架构审阅，不是模型效果或性能收益认证。图用于解释判断，代码、Schema、设备输出与评测报告仍是权威事实。

本报告的 8 张中文配图均由图像生成模型逐张生成，不是 Mermaid、SVG 或代码绘图；图片按仓库约定保存在 `docs-assets` 分支，`main` 只保留文档引用。英文原图仅作为早期审阅记录保留，报告默认展示中文版本。

## 1. 执行摘要

本项目已经越过“网页里调用一次大模型”的阶段，形成了一个面向 OpenHarmony 异构优化的**专用 Agent Harness**：模型在统一 Session 中推理，平台用持久化 Run 管理事实，通过类型化 MCP/Action 操作页面、构建环境和设备，并把静态分析、硬件/API 知识、严格异构案例和真机模板编入工作流。

当前最强的四项能力是：

1. **证据分级闭环**：不会把模型建议、编译成功、设备零退出码、功能等价和性能收益混成一个“验证成功”。
2. **类型化工具边界**：模型只能请求已注册 Action，执行器、构建 Profile、HDC 路径和宿主机权限由平台控制。
3. **可恢复、可观测运行时**：Run、事件、检查点、幂等结果、SSE 游标和脱敏 Trace 已形成系统骨架。
4. **领域化 Harness 资产**：25 个版本化 Skill、分层知识库、API Top-N、严格 CPU fallback 案例和受审查模板共同提高了专业判断能力。

下一阶段最值得投入的四项工作是：

1. **持久化执行平面**：把进程内线程、waiter、临时凭证和任务占有关系升级为可租约、可协调、可重放的执行平面。
2. **统一契约与事件模型**：从一份版本化 Action/Event Registry 生成 MCP、HTTP、UI、校验、审计和测试适配器。
3. **端到端评测飞轮**：把已有 60 条策略样本、17 条路由样本和生产失败 Trace 升级为隔离多次运行、轨迹评分和发布门禁。
4. **代码知识与上下文工程**：面向选定模块建立语法、语义、设计三层证据图，用任务规划检索替代主要依赖文本截断的压缩。

## 2. 什么是 Agent Harness

本报告把 Agent Harness 定义为“模型之外、决定 Agent 能否稳定完成真实工程任务的全部系统”：

- 会话、任务状态机、检查点、恢复和取消；
- Prompt、Skill、上下文装配、压缩和长期记忆；
- 工具协议、权限、执行器、幂等和副作用管理；
- Build/Device 等真实环境适配；
- Artifact、事件、Trace、评测、回归和发布治理。

这个定义与业界经验一致：长任务需要结构化进度和可恢复交接；工具接口本身会显著影响 Agent 成功率；评测应同时检查最终结果与执行轨迹，而不是只看最终文本。参考 [Anthropic 长任务 Harness](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)、[SWE-agent 的 Agent-Computer Interface 研究](https://arxiv.org/abs/2405.15793) 和 [Anthropic Agent Evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)。

## 3. 审阅证据

| 项目事实 | 当前快照 |
|---|---|
| Skill Registry | 25 个 Skill；每项有版本、claim boundary、required evidence、quality gate、fixture 和演进事件 |
| 模型策略评测 | 60 条 Hardware-KB 分层样本；含 smoke、development、validation、holdout |
| 语义路由评测 | 17 条变换样本；覆盖改名、等价改写、负例和 2 条 holdout |
| 自动化测试 | `python -m unittest discover -s backend/tests`：314 项通过，2 项条件跳过 |
| 可恢复事实面 | `run.json` 检查点、`events.jsonl`、`analysis_execution_trace.jsonl`、页面级主 Session 归属与分析工件引用 |
| 工具边界 | UI、Build、Device 三类 owner；Per-Run MCP；固定 Profile；设备租约与 Bridge |
| 当前部署约束 | README 仍明确使用 `uvicorn --workers 1`；活动执行线程与 waiter 位于进程内 |

证据等级：下文的“已实现”来自代码或可执行测试；“推断”来自跨模块结构审阅；“建议”是目标设计，不表示已经落地。

## 4. 总览

| # | 判断 | 类型 | 优先级 | 核心结论 |
|---|---|---|---|---|
| 1 | 证据优先闭环 | 优势 | 保持 | 项目最有辨识度的能力，是把建议逐级变成可审计事实 |
| 2 | 类型化工具与安全边界 | 优势 | 保持并收紧 | 模型有行动力，但不能任意选择执行器或宿主机权限 |
| 3 | 可恢复与可观测 Runtime | 优势 | 保持 | 已具备事件、检查点、幂等、SSE 和脱敏 Trace 骨架 |
| 4 | 领域化 Harness 资产 | 优势 | 持续积累 | Skill、KB、严格案例和模板让系统区别于通用聊天 Agent |
| 5 | 持久化执行平面 | 改进 | P0 | 文件可恢复不等于任务调度、waiter 和租约可跨进程恢复 |
| 6 | 契约与事件模型收敛 | 改进 | P1 | 多处重复声明会造成 Schema、文档、审计和兼容行为漂移 |
| 7 | 生产级评测飞轮 | 改进 | P1 | 已有好样本，但尚需统一的端到端多试次发布门禁 |
| 8 | 代码知识与上下文工程 | 改进 | P1 | 30 GB 代码仓需要模块级证据图与任务感知检索，而非继续堆 Prompt |

## 5. 做得好的地方

### 5.1 证据优先的闭环设计

![证据优先闭环](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/agent_harness_insight/01_evidence_first_closed_loop_zh.png?raw=1)

**判断：这是目前最值得保留的架构原则。**

模型输出首先是“假设”；之后依次取得受控构建、设备执行、功能等价、硬件参与、性能/功耗收益证据。每一级都不能替代下一级。项目在 [README](../README.md#L85) 中明确拆开构建、上传、零退出码、功能等价和收益；Runtime 还要求真机任务在完成前出现真实 `device.execute_shell` 结果，[否则拒绝把任务标成完成](../backend/agent_runtime.py#L1972)。

这比直接让模型生成一段 GPU/NPU 代码更可靠，原因有三点：

- 它把“模型可能知道什么”与“平台实际观察到什么”分开；
- 编译、部署和执行失败都成为下一轮可复用事实，而不是被自然语言吞掉；
- 最终结论可以精确限定到设备、Profile、输入和实验范围。

**边界**：E0 最小实验通过只证明该实验，不证明真实热点已集成或有端到端收益。后续应继续保持 E0/E1/E2 分层，不能为了“结果好看”缩短证据梯子。

### 5.2 类型化工具和安全边界

![类型化工具与安全边界](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/agent_harness_insight/02_typed_tool_security_boundary_zh.png?raw=1)

**判断：工具边界与任务域匹配得很好。**

[Runtime Action Router](../backend/runtime_action_router.py#L51) 对 Build、Device 和 UI Action 声明 owner、输入要求和执行路径；[Per-Run MCP](../backend/kimi_mcp_server.py#L159) 负责页面、平台工件和 Windows HDC 跨边界能力，内部 Action API 要求绑定 Run bearer token。Kimi 的后端源码导航、文件、Shell、编译和同机 HDC 使用原生 Coding 工具；需要固定 Profile 证据时再进入 Build Worker。结构化推理阶段可临时关闭工具，`WebSearch/FetchURL` 在所有平台 Prompt 中保持禁用。

这种设计的价值不是单纯“限制模型”，而是建立了更好的 Agent-Computer Interface：

- Action 名称与工程意图一致，模型不需要拼装底层命令；
- 参数校验和 executor ownership 将策略与执行分离；
- Artifact ID、幂等键和审计事件为恢复与复盘提供稳定主键；
- UI、Linux 构建端和 Windows/本机设备端可以独立演进。

这与 MCP 对显式用户控制、工具边界和安全传输的要求一致；未来多用户化时还应采用资源绑定、短期 token 和禁止 token passthrough 的原则。参考 [MCP Authorization](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) 和 [OpenAI Agents SDK Guardrails](https://openai.github.io/openai-agents-python/guardrails/)。

### 5.3 可恢复、可观测的 Agent Runtime

![可恢复与可观测 Runtime](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/agent_harness_insight/03_recoverable_observable_runtime_zh.png?raw=1)

**判断：系统已经有了长期任务所需的正确骨架。**

[AgentRunStore](../backend/agent_runtime.py#L683) 保存紧凑可变检查点和 append-only 事件；每个事件同时写入内部日志与可导出的 `analysis_execution_trace.jsonl`，[事件有稳定 sequence](../backend/agent_runtime.py#L884)。Runtime 将相同 Action 的输入哈希作为幂等身份，并把失败结果也记录为事实，[避免恢复后无限重复同一副作用](../backend/agent_runtime.py#L1013)。交互式 Kimi CLI 由 `page_session_id` 固定归属一个 Run，`analysis_record_id` 只作为其中可切换的审计上下文；[AnalysisAgentSessionRegistry](../backend/analysis_agent_session.py#L34) 仅保留给兼容结构化调用和隔离基线。浏览器使用 SSE cursor 续接事件，[AnalysisTraceStore](../backend/analysis_observability.py#L60) 补齐跨模型请求、页面动作和合同处理的脱敏审计。

这使以下场景有明确语义：

- 浏览器刷新后重新读取 Run，而不是从头分析；
- HTTP 429、输出截断或 Bridge 离线时保存精确恢复点；
- 用户停止后阻止后续工具，已完成证据不回滚；
- 同一个页面/标签页保持同一 Kimi Session 与 Platform Run，分析记录切换不打断上下文；新页面建立独立 Session。

业界持久化图运行时也强调 step checkpoint、pending writes、interrupt/resume 和幂等副作用；项目已经覆盖了其中大部分概念。参考 [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 与 [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)。

### 5.4 领域化 Harness 资产

![领域化 Harness 资产](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/agent_harness_insight/04_domain_specialized_harness_assets_zh.png?raw=1)

**判断：这是平台相对通用大模型推理最核心的竞争力。**

[Skill Registry](../backend/skills.py#L543) 当前登记 25 个 Skill，覆盖 Trace、源码定位、AST、KB 检索、硬件路由、API 召回、自建 API 知识构建、能力探测和真机实验；每个 Skill 通过 [Skill Governance Profile](../backend/skills.py#L1113) 声明结论边界、证据、失败场景与版本演进。仓库同时部署了：

- 优化、硬件、NPU/GPU/DSP/媒体 API 知识库；
- 40 条严格用户态/三方库异构到 CPU/software fallback 案例；
- 可追溯 SHA-256 的受审查硬件 API 模板；
- 60 条 Hardware-KB 策略样本与 17 条模型路由变换样本；
- CPU oracle、Build Profile 和真机结果协议。

这些资产使 Agent 不只靠参数知识“猜硬件”，而是先建立代码画像，再选硬件目录、召回 API Top-N、检查暴露层和运行时条件，最后设计最小可证伪实验。它们提供的是**领域语义和证据约束**，不是自动证明；这个边界目前把握得较好。

自建 API 知识库进一步验证了一个重要 Harness 分层原则：**确定性工具决定模型能看见什么，Skill 决定模型如何使用这些事实**。扫描器必须先把公开函数合同、算子/能力枚举、内部实现和示例分层，并通过类别配额与质量门禁保证 Top-N 主体可用于 API 选择；模型负责解释、补全抽象和编排后续动作。若候选集中只有 `Service`、`Manager` 或实现辅助类，仅修改提示词不能恢复从未进入上下文的 `OH_NN*` 函数和 `OH_NN_OPS_*` 算子。此类修改必须同步演进扫描器版本、Skill、MCP/Action 摘要、UI 质量状态，并通过合成干扰样本与代表性真实仓双层回归。

## 6. 可以优化的地方

### 6.1 P0：从可恢复文件升级为持久化执行平面

![持久化执行平面升级](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/agent_harness_insight/05_durable_execution_plane_upgrade_zh.png?raw=1)

**事实**：Run 和事件已经持久化，但活动执行仍由进程内 `threading.Thread` 驱动，[UI waiter、cancel event 和线程表也在内存](../backend/agent_runtime.py#L988)；进程重启后 Runtime 会把任务转成等待恢复状态，[而不是继续原来的活动调用](../backend/agent_runtime.py#L1082)。当前部署明确要求 [Uvicorn 单 worker](../README.md#L31)。这对单用户 Phase A 是合理取舍，但它不是多进程或高可用执行平面。

**风险推断**：

- 进程退出时，活动模型流、同步 UI waiter 和临时凭证需要重新建立；
- 多 worker 下，同一 Run 的线程所有权、SSE 唤醒和 UI 回传无法仅靠进程内对象协调；
- Build/Device 外部任务可能已继续执行，平台需要 reconcile，而不能只根据本地线程状态判断；
- 文件锁可以保护单次写入，但不能完整表达任务 claim、租约过期和 outbox 投递。

**建议目标**：保留现有 Run/Action/Artifact 契约，在后端加入轻量持久化调度层：

1. `task`、`lease`、`attempt`、`outbox`、`wait_subscription` 和 `idempotency` 持久化；
2. Model、Build、UI Wait、Device 变为可 claim/renew/reconcile 的无状态 worker；
3. 所有外部副作用先写意图与幂等键，再执行，再原子记录结果；
4. Bridge 重连后按 job ID 对账，不依赖原进程内 waiter；
5. 先支持单节点多进程，再评估是否需要独立队列产品。

**验收标准**：在模型流、Build、UI 等待和 Device Job 四个阶段分别强制杀进程，重启后不丢事实、不重复副作用，并能继续或明确暂停。

### 6.2 P1：统一 Action、MCP、HTTP、UI 与事件契约

![契约与事件模型收敛](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/agent_harness_insight/06_contract_event_model_convergence_zh.png?raw=1)

**事实**：当前 Action 信息分布在 `runtime_action_router.py`、`kimi_mcp_server.py`、`skills.py`、FastAPI request model、前端 UI action schema 和多份 Spec 中；可观测性又同时存在 `agent_run_event/v1` 与 `analysis_execution_trace_event/v1` 两个相近事件模型。兼容代码还保留了 legacy 浏览器 Device Action 与新 Build/Device 链的映射。本次审阅还发现总体文档的 Skill 数量从 21 漂移到实际 22，已一并修正。

**风险推断**：新增一个 Action 时，容易出现 MCP 已暴露但 UI 不认识、Router 校验与文档不一致、审计字段缺失或旧客户端静默降级。现在的测试能发现许多问题，但重复声明会持续增加维护成本。

**建议目标**：建立一个可执行的 `action_contract_registry/v1`，每个 Action 只声明一次：

- ID、版本、owner、输入/输出 JSON Schema；
- mode/permission、确认策略、secret/redaction 字段；
- 幂等语义、超时、取消和恢复语义；
- 输入/输出 Artifact 类型与事件映射；
- legacy alias 与兼容期限。

由 Registry 生成 MCP wrapper、OpenAPI/Pydantic model、前端 descriptor、Runtime validator、审计投影、文档表格和 contract fixture。事件侧再定义一个 canonical envelope，至少包含 `event_id/type/version/time/actor/correlation/causation/run/action/artifact/payload`；产品 Trace 可以是其投影视图，而不是第二套事实源。

### 6.3 P1：把现有评测资产升级为生产发布飞轮

![生产级评测飞轮](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/agent_harness_insight/07_production_eval_flywheel_zh.png?raw=1)

**事实**：项目的评测基础高于一般原型。Hardware-KB 有 60 条分层 evaluator-side oracle；模型路由有 17 条变换样本；评分器区分 context preflight、硬件判断、CPU 并行、校准和验证门禁。现有规范也明确指出 17 条套件只有 2 条 holdout，[不能声称跨仓库强泛化](hardware_kb_context_strategy_evaluation_spec.md#L93)。

**仓库审阅未发现**统一 CI 发布门禁、同一 case 的隔离多试次模型矩阵、完整 Agent 轨迹评分，或把生产失败 Trace 自动转成去重失败语料的闭环。因此目前更接近“优秀的领域单元评测”，还不是“整个 Harness 的持续发布系统”。

**建议目标**：

1. 建立不可变 `eval_run_manifest`，锁定代码、Skill、KB、模板、模型、参数、设备和 case hash；
2. 每个 case 独立 Session，多次运行，避免上下文污染并测量方差；
3. 同时使用确定性结果 grader、轨迹/策略 grader 和少量人工复核；
4. 分开 capability suite、regression suite、template-enabled production suite 和 template-blind holdout；
5. 指标至少包含 precision/recall、拒绝特异性、置信度校准、任务完成率、工具错误、P50/P95 时延、Token、恢复成功率和设备证据闭合率；
6. 从生产 Trace 聚类失败，但必须脱敏、去重、冻结并防止直接泄漏到 Prompt/KB；
7. Skill/KB/模板/Runtime 任一版本变化都触发对应最小回归集，核心版本发布触发全量矩阵。

Agent 评测不应要求模型走唯一固定路径；应该严查结果、危险行为和关键策略边界，同时允许不同的合理轨迹。这与 [Anthropic Agent Evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) 提出的 transcript 阅读、多类 grader 与正负样本平衡一致。

### 6.4 P1：建设模块级代码知识与损失感知上下文

![代码知识与上下文工程](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/agent_harness_insight/08_code_knowledge_context_engineering_zh.png?raw=1)

**事实**：平台已经做了结构化 context projection、代码候选范围、Token 预算和 compaction。浏览器会保留证据 ID、Trace、源码特征和选中硬件；后端兼容 Runtime 在接近窗口时保留近期消息，并把更早内容压成文本摘要，[当前核心算法仍是长度估算、截取和拼接](../backend/agent_runtime.py#L1560)。这在单模块短任务上可用，但不能替代代码语义索引。

**风险推断**：当热点只在 callee 中、调用链跨多个目录、同名符号很多或 30 GB 仓库持续变化时，纯文本摘要容易丢失调用因果、源码版本和缺失证据；继续增加 Prompt 只会提高 Token、延迟和噪声。

**建议目标**：仅对用户选定模块建立三层、可增量更新的代码知识库：

| 层 | 确定性数据 | 模型增强数据 | 主要用途 |
|---|---|---|---|
| 语法层 | Tree-sitter/Clang AST、符号、调用图、数据流、读写与源码 hash | 无或仅作解释 | 精确定位、跨过程计算核解析、变更影响 |
| 语义层 | 注释、测试、类型、错误码、Commit 关联 | 函数意图、契约、不变量、数据所有权摘要 | 解释热点、约束并行/异构改写 |
| 设计层 | 目录依赖、构建 target、owner、ADR 链接 | 模块边界、关键流程和架构模式 | 确定实验边界、集成路径与风险 |

查询时由 `hotspot + trace + user intent + selected unit` 生成检索计划，优先取得核心代码、必要 callee、数据依赖、相关 API、反证和缺失证据；大正文保留为 Artifact 引用。Compaction 必须保留 provenance、源码位置、证据 ID、未解决目标和失败动作，而不是只保留自然语言摘要。

实现顺序以“用户选定目录 + C/C++/ArkTS + 优化任务需要的边类型”为起点，逐步加入 import graph、依赖分批、fingerprint/staleness、增量更新和结构化图存储，并为每种检索问题建立 Recall@K 与证据完整性评测。

## 7. 推荐实施顺序

| 阶段 | 首要交付 | 原因 |
|---|---|---|
| M1 | Action Contract Registry + canonical event envelope | 先降低继续加功能时的契约漂移，给后续调度和评测统一数据面 |
| M2 | Durable task/lease/outbox + kill/restart 测试 | 解除单 worker 和进程内 waiter 对长期任务的约束 |
| M3 | E2E eval manifest、隔离多试次、轨迹 grader、发布门禁 | 让 Skill/KB/Runtime 的修改有可量化回归依据 |
| M4 | 选定模块的 syntax graph 与跨过程 compute-core resolver | 先解决热点 callee、调用链和大仓检索的高价值问题 |
| M5 | 语义/设计层和 loss-aware context planner | 在确定性图稳定后再引入模型摘要，避免生成内容成为错误事实源 |

不建议当前立即更换 Agent 框架。现有 Kimi Engine、Run、MCP 和设备链已经包含大量领域合同；优先吸收成熟框架的 checkpoint、interrupt、task lease、trace 与 eval 方法，同时保持 `AgentEngine` SPI 可替换，比整体迁移风险更低。

## 8. 验收指标

| 方向 | 建议验收指标 |
|---|---|
| 执行可靠性 | 四阶段 kill/restart 恢复率；重复外部副作用为 0；孤儿任务可 reconcile |
| 契约一致性 | Registry 生成覆盖率 100%；手写重复 schema 持续下降；兼容测试覆盖所有 active/legacy 版本 |
| Agent 质量 | 分层 precision/recall/校准；任务完成率；危险工具调用率；轨迹策略违规率 |
| 运行效率 | P50/P95 首 token、总时延、工具等待、input/output/cache Token、compaction 次数 |
| 真机闭环 | build/deploy/execute/correctness/hardware/performance 各级证据闭合率，禁止跨级升级 |
| 代码知识 | 符号/调用边 Recall@K、热点 callee 命中率、源码 hash 新鲜度、上下文证据保留率 |

## 9. 总体判断

这个项目的方向是对的，而且已经形成了几项不容易复制的工程资产。它最有价值的地方不是“模型能不能说出 GPU/NPU/DSP”，而是把代码、知识、工具和设备组织成一个可追溯的科学实验过程。

下一阶段应从“继续增加单点能力”转向“收敛控制平面”：统一契约、持久调度、完整评测和模块级证据图。完成这四项后，平台才会从强单用户工程原型稳定迈向可长期维护、可多人部署、可证明改进的工业级 Agent Harness。

## 10. 外部参考

- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Anthropic: Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [OpenAI Agents SDK: Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- [OpenAI Agents SDK: Guardrails](https://openai.github.io/openai-agents-python/guardrails/)
- [Model Context Protocol: Authorization](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
- [SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering](https://arxiv.org/abs/2405.15793)
