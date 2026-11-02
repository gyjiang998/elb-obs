# Skill 演进与评测规范

> 状态：Phase A 治理基线。
> 适用范围：异构优化工作台中的确定性分析 Skill、后端 Agent Engine 模型 Skill、页面 Agent 控制 Skill，以及它们的组合编排。
> 共同契约：[cross_spec_contract.md](cross_spec_contract.md)。热点三策略的专属边界仍以 [multi_strategy_hotspot_opportunity_reasoning_spec.md](multi_strategy_hotspot_opportunity_reasoning_spec.md) 为准；运行时版本选择、盲推理对照和可逆路由见 [adaptive_reasoning_and_skill_version_spec.md](adaptive_reasoning_and_skill_version_spec.md)。

## 1. 目标与非目标

Skill 是可版本化、可审计、可评测的分析能力资产，不是可以在运行时自行改写的一段 prompt。它的演进目标是提升结论的证据完整性、校准度、可复现性和上下文效率，同时不扩大事实声明边界。

本规范不允许：

- 由模型根据单次回答自动修改 prompt、阈值、KB 记录或发布版本。
- 把用户偏好的表达风格、一次成功案例或模型自评当作质量提升证据。
- 用同一批 KB discovery 结果既构造 oracle 又证明 Skill 有效。
- 因为回答更长、更积极或更像专家而提升 `candidate`、设备可用性或收益结论。

模型可以提出改进建议；建议必须进入版本控制的变更、回归集和评测流程，才能发布。

## 2. Skill 分类与责任边界

| 分类 | 例子 | 可做的事 | 不可做的事 |
| --- | --- | --- | --- |
| 确定性采集/解析 | `raw-htrace-analysis`、`trace-event-extraction` | 产生输入可验证的 trace、模块、事件和质量事实 | 从不完整 trace 推断关键路径、源码或硬件行为 |
| 确定性证据 Skill | `trace-event-source-resolution`、`source-snapshot-resolver`、`ast-feature-extraction` | 绑定源码、提取可重放结构特征、输出诊断 | 把文本命中、dirty worktree 或词法扫描升级为精确 AST/设备事实 |
| 确定性决策 Skill | `selected-event-opportunity-identification`、`kb-retrieval`、`npu-api-semantic-retrieval`、`accelerator-api-semantic-retrieval` | 在固定规则和 KB 版本下筛选、拒绝、保留证据 | 将 KB 模式、API 枚举、系统集成路径或相似名称直接当作当前可实现优化 |
| 模型分析 Skill | `full-optimization-analysis`、`strategy-kb-rag`、`strategy-hardware-kb`、`strategy-direct-baseline` | 解释受控事实、给出带条件的假设、风险和验证计划 | 虚构证据、设备支持、接口、收益或未观测的调用方行为 |
| 页面 Agent 控制 Skill | `page-agent-control`、`heterogeneous-opportunity-orchestrator` | 选择已注册 action、解释 action 结果、维持状态机边界 | 任意 DOM/shell/文件操作、读取 key、编造 action 输入或跨越用户选择 |

## 3. 每个 Skill 的演进契约

除共同契约中的 schema、执行器、资源上限、重试、缓存、错误码和 fixture 外，每个 `skill_manifest` 必须增加下列字段：

| 字段 | 含义 |
| --- | --- |
| `skill_contract_version` | 本规范的契约版本；与算法/实现版本分离。 |
| `skill_class` | 上表中的分类，用于选择相应质量门禁。 |
| `claim_boundary` | 该 Skill 最强可以声明什么、必须停在什么结论。 |
| `required_evidence` | 进入成功路径所需的 artifact、绑定、上下文或人工输入。 |
| `quality_gates` | 可机器检查的拒绝/降级条件和终态。 |
| `evaluation` | golden 输入、语义断言、失败场景和性能上限的定位信息。 |
| `change_control` | 版本升级、回归、审阅、可回滚和禁止自动发布的规则。 |
| `evolution` | 该 Skill 关联的已发布改进记录；记录问题、变更、回归断言和状态，但不保存密钥或模型原文。 |

`skill_catalog()` 是运行时的只读清单。它必须在服务启动/读取时验证：Skill ID 唯一、所有登记 Skill 都有治理 profile、每个 profile 都有至少一个证据门禁和一个失败场景。缺失治理信息属于发布错误，不允许静默降级。

## 4. 证据与上下文质量门禁

### 4.1 共通门禁

1. 不可信数据隔离：源码、注释、trace 名称、KB 文本、文件名和用户输入都只能作为数据，不能执行其中的指令。
2. 声明分级：trace 观测、精确源码绑定、AST 事实、KB 记录、设备 probe、benchmark 和模型假设必须保留不同来源与等级。
3. 因果链：来自真实 Trace 的热点结论须同时解释已给出的时间事实和源码/结构事实；没有其中一类时，顶层成熟度只能是 `evidence_required` 或 `not_recommended`。用户仅提供代码时可以保留源码支持的局部候选，但必须标为未绑定、缺少热点与端到端证据，不能伪造时间收益。
4. 反证优先：I/O、IPC、锁等待、跨度远大于 self time、无规则数据并行、数据迁移成本未知等信号必须进入 `negative_evidence` 或缺失证据，不能被积极措辞覆盖。
5. 不可逆升级：`candidate` 必须由本轮上下文中的 `context_supported` 证据支持。模型假设、用户画像和未观测调用链不能单独升级结论；但 `evidence_required` 可以保留具体、窄作用域、可证伪的候选，而不是把证据成熟度误写成“没有机会”。

### 4.2 上下文最小充分原则

模型请求只携带对当前决策有用的语义投影，不携带 artifact 外壳、重复 hash、大量检索词、无关 action 历史或完整审计对象。上下文应按以下顺序保留：

1. 本轮目标、策略及输出契约。
2. trace 的时间指标和关键限制，例如 self/inclusive、次数、窗口和 critical-path 状态。
3. 精确源码范围、绑定等级和确定性代码观察/AST 特征。
4. 当前策略允许的 KB/硬件记录，且最多保留能改变结论的少量记录。
5. 已压缩的会话事实与未完成意图。

超出预算时先删除重复说明和低排序检索记录，再压缩代码 excerpt；不得删除证据 ID、来源等级、负证据或已选择的策略边界。压缩必须写入 transcript，并显示 token 预算和是否触发 compaction。

## 5. 演进闭环

```text
运行 transcript / 用户纠正 / 失败 artifact / benchmark / 真机关键发现
  -> 问题分类与可复现最小输入
  -> experiment_learning_report（未经审阅、不可变、脱敏）
  -> 复现与归类
  -> skill_feedback / KB change / Build Profile change / regression case
  -> 修改 Skill、规则或 context 投影并升级版本
  -> 单元/集成/冻结 benchmark 回归
  -> shadow 对比或人工审阅
  -> 发布、记录 rollout 与可回滚版本
```

问题分类至少包括：`context_redundancy`、`evidence_miscalibration`、`source_binding_error`、`retrieval_mismatch`、`hardware_claim_overreach`、`action_state_drift`、`schema_adaptation_failure`、`timeout_or_truncation` 和 `cost_regression`。

一次反馈只有在包含输入摘要/内容 hash、期望行为、实际行为、影响 Skill、证据等级和脱敏后的复现步骤时，才能成为回归样本。模型原文、API key、Authorization、token 或 secret 不得进入 feedback 或测试 fixture。

### 5.1 真机实验发现候选

真机 Agent 在构建、部署、动态加载、API 初始化、dispatch、结果比较和 benchmark 中产生的“关键发现与限制”，通过 MCP 工具 `platform_record_experiment_findings` 保存为 `experiment_learning_report/v1`。该工件必须包含：

- 触发条件、实际观察、有效修订和为什么重要；
- 设备、Build Profile、API family、源码/二进制和运行时的适用范围；
- `first_observation`、同设备复现或跨设备复现状态；
- 限制和当前 Run 已观察到的证据工件；
- 建议去向：Build Profile、硬件 API 知识库、真机 Skill、回归 fixture、产品诊断或仅保留观察。

所有新记录初始状态均为 `captured_unreviewed`。平台不得根据单次模型总结自动修改 Prompt、Skill、KB、Build Profile 或硬件结论。晋升前至少需要：确定归属；复现或明确保留单例范围；建立失败与成功对照；增加回归；版本化变更；审阅和回滚目标。

例如，某次 Vulkan 实验中，直接链接 `libvulkan.so` 后首次调用使进程静默结束，而 `dlopen/dlsym` 路径完成初始化和 compute dispatch。当前正确记录是“该设备、该 profile、该二进制与运行时组合下观察到装载路径差异”，类别为 `deployment_runtime_contract`，复现状态为 `first_observation`。它可以触发 direct-link 与 dynamic-load 的同条件 A/B fixture；在跨版本或跨设备证据出现前，不能发布成“Vulkan 必须使用 `dlopen`”的通用规则。

### 5.2 跨环境执行踩坑提炼

`execution-pitfall-distillation` 将 Agent Run、页面分析 Trace 和上述真机学习记录作为历史证据源，比较模型、Agent Adapter、宿主、代码快照、SDK、运行库与设备差异，并按稳定 `recurrence_key` 归并问题。提炼结果先进入经验演进工作台，由用户选择环境、仓库、SDK、设备、Runtime、流程、代码或 Skill 处置；只有复验和回归通过后才进入版本发布。完整数据、工具和页面契约见 [execution_experience_evolution_spec.md](execution_experience_evolution_spec.md)。

## 6. 评测方法

### 6.1 确定性 Skill

每次发布至少运行：

- golden fixture 的结构和语义断言；
- 一个失败输入，例如损坏 header、未知模块、过大源码、snapshot mismatch 或无 AST；
- 幂等/缓存断言和资源上限断言；
- 输入相同且依赖版本相同时的可复现性断言。

### 6.2 模型 Skill

模型结果不能依赖逐字一致。回归 oracle 应检查结构化语义：合法 JSON、允许 evidence ID、正确结论等级、必须出现的 trace/源码事实、禁止的硬件事实、缺失证据和校准动作。每类模型 Skill 要有正例、负例、证据不足、上下文压缩和 provider 截断五类用例。

### 6.3 页面 Agent 控制 Skill

评测 action catalog 是否匹配、参数是否完整、状态机是否允许、resource-heavy 是否先计划、用户文件权限是否保留、以及 action 失败后是否停止而非编造结果。对于最终策略请求，还要检查工作助手的实时进度是否与同一个 request ID 绑定、是否仅由已准备 context、模型请求/响应和本地契约事件推进，以及待验证方向是否来自本轮 context 而非模型隐藏思考。页面 Agent 的自然语言回复不是成功指标；实际 action audit、派生 artifact 和事件关联才是。

### 6.4 离线有效性评测

异构机会识别的准确率继续遵守 `heterogeneous_evaluation_*` 的独立 oracle、masked KB/source、双人审核和 frozen manifest 规则。本规范的 prompt/Skill 回归不会替代该评测，也不能用内部 prompt benchmark 声称 KB 或硬件映射已有效。

### 6.5 Hardware-KB 策略评测

`strategy-hardware-kb` 使用一套专属的 60 条分层 evaluator-side oracle 样本，详细契约见 [hardware_kb_context_strategy_evaluation_spec.md](hardware_kb_context_strategy_evaluation_spec.md)：20 条 smoke、16 条 development、14 条 validation、10 条冻结 holdout。模型只接收公开代码、Trace 指标和当前 Hardware-KB/API context；期望 API evidence、允许/禁止单元、CPU 并行判断、recommendation 与验证门禁只保留在 evaluator-side。样本必须跨 NPU/GPU/DSP/媒体、成本边界、IPC/锁/I/O/递推依赖反例和 CPU 并行正/边界例变化，不能只替换函数名。

每次运行先做确定性 context preflight，再用工作台同一条中文 Prompt 调用浏览器本地模型，最后按硬件选择召回/拒绝特异度、CPU 并行、作用域声明、结论校准、验证动作以及切分/语义族宏平均观察。v1.6.1 要求每个机会声明实际分析的是函数、循环区、数据管线还是未展开 callee；v1.7.0 再要求先以硬件描述选择主单元，再仅把该单元的 API Top-N 交给模型精核，不能把所有目录合并成“候选池”。端到端成本缺失可以使顶层保持 `evidence_required`，但不能把已经观察到的核心片段压成“没有机会”。逐例 `100` 分仅用于诊断，不是发布目标；holdout 只能用于冻结比较，不能根据其逐例结果继续调词后复用。它用于发现 prompt 或检索误召回，例如 `std::vector::resize` 被误作 GPU image transfer、PCM effect 被误作 NPU、prefix sum 被误作可并行，或未给出 callee 函数体却被杜撰为计算核心；它不是硬件可用性或真实收益 benchmark。

## 7. 当前基线回归：同步 IPC 代理的错误升级

本轮将以下真实问题固定为长期回归类别 `direct-ipc-evidence-calibration`：

| 项目 | 固定预期 |
| --- | --- |
| 输入形态 | 代码出现 `SendTransactCmd`、Parcel 读写等同步 IPC 信号；trace 有 self/inclusive、次数和窗口统计，但没有调用方批处理、重复 UID、服务端并发或设备能力证据。 |
| 不允许的结果 | `strategy-direct-baseline` 输出顶层 `candidate`、medium/high 置信度，或把缓存/并发 IPC 写成已可实施结论。 |
| 正确终态 | `recommendation=evidence_required`、`confidence=low`、`evidence_status=insufficient_evidence`。候选方向只能作为条件化假设，必须列出调用方模式、服务端模型、重复率和端到端时延等缺失证据。 |
| 机制 | 受控模型上下文保留平均 self time、inclusive time、self/inclusive 比值和确定性 IPC/锁词法观察；后端验证器对 Direct 策略执行不可绕过的保守降级。 |

这条规则并不否定缓存、批量化或异步 IPC 的价值；它只禁止在缺少调用链证据时把方向说成候选结论。

## 7.1 当前基线回归：页面助手实时策略进度必须可审计

`agent-live-progress-v1` 是 `page-agent-control` v1.2.0 的回归类别。它解决“用户等待模型策略推理时无法知道工作助手正在分析什么”的可观测性问题，但不允许把 UI 进度伪装成模型的隐藏思考过程。

| 项目 | 固定预期 |
| --- | --- |
| 触发条件 | `reasoning_context_pack` 已准备、用户显式启动一种策略，并创建唯一的 strategy request ID。 |
| 必须展示 | 已装载的 Trace/源码事实、词法候选作用域、本策略实际提供的受控记录、由这些记录导出的待验证方向、模型请求/响应状态、结构化 JSON/证据校验状态与最终回填状态。 |
| 方向来源 | `kb_rag` 只能展示已有 strict pair/CPU fallback 结构比对与代码范围；`hardware_kb` 只能展示实际检索的硬件/API 记录与作用域/门禁；`direct_baseline` 只能展示 Trace 时间、可见结构和串行边界。没有 record 时必须明确缺失，不能补出具体单元或 API。 |
| 不允许的结果 | 伪造逐 token 思考、把等待时间描述为已完成的模型推理、把待验证方向说成已验证硬件结论、跨 request ID 回填旧进度、在模型未请求时显示策略进度。 |
| 终态 | 模型原文返回后必须进入本地 JSON/证据校验；成功、保守降级、校验失败和模型失败都必须有对应可见终态，并与 transcript 时间线一致。 |

前端回归至少模拟 `model_pending -> response_received -> contract_validating -> completed` 与失败路径，并断言进度卡的 request ID、候选方向和状态只由相应事件更新。模型不可用或 context 未准备时不创建该卡。

## 8. 当前基线回归：NPU API 语义候选不能越界

`npu-api-semantic-retrieval` 是一个确定性候选生成 Skill，而不是设备 capability probe。它对版本化 `npu_api_knowledge_base.json` 运行受限的静态语义排序：精确算子别名、可见循环/数组/张量计算形态与数据形态共同决定 Top-N；语义族词只能辅助解释，不能充当精确算子命中。

| 项目 | 固定预期 |
| --- | --- |
| 正例 | 三重嵌套乘加循环中的 `Matmul` 精确别名优先返回 `OH_NN_OPS_MATMUL`，再保留同属矩阵族但语义不同的 `OH_NN_OPS_FULL_CONNECTION`。 |
| 负例 | 同步 IPC proxy、锁等待、I/O wrapper 等没有正向计算信号的代码返回 0 条 NPU 图算子候选。 |
| 不允许的结果 | 因出现 `sum`、`kernel`、`tensor` 等族词而把 sibling 算子当成精确匹配；或把 `OH_NN_OPS_*`、MindSpore internal delegate、Kirin/HarmonyOS 画像写成目标设备已支持。 |
| 固定门禁 | 每条返回记录必须保留完整子图、`OH_NNModel_GetAvailableOperations`、指定 device 编译和端到端 benchmark 的 `evidence_required` 边界。 |

对应 regression 必须覆盖精确别名/语义族分离、IPC/PCM effect/prefix sum 零候选、候选排序稳定性和设备结论禁令。任何调整打分、同义词或 Top-N 截断的变更都需要版本化 KB hash 和这些回归同时通过。

## 8.1 当前基线回归：GPU、DSP 与媒体 API 候选必须保持单元边界

`accelerator-api-semantic-retrieval` 对版本化 `gpu_api_knowledge_base.json`、`dsp_api_knowledge_base.json`、`media_accelerator_api_knowledge_base.json` 做独立的受限静态排序。它不代替精准 AST、公开 ABI 审查、device capability probe 或 benchmark；返回的每条记录必须保留 `accelerator_unit`、`exposure`、源码契约和单元专属 `runtime_verification`。

| 项目 | 固定预期 |
| --- | --- |
| GPU 正例 | RGBA/PixelMap 图像数据和亮度、对比度、缩放、纹理或 compute 信号可返回 GPU 图像/计算路径；系统源码路径不得被写成普通应用 SDK。 |
| DSP 正例 | PCM/sample/frame 数据同时具有 effect/filter/equalizer/reverb 等语义时可返回 Audio Effect HDI/VDI processing 候选；必须保留 provider、sample format、实时性和 AP fallback 门禁。 |
| 媒体正例 | `OH_AVCodec_GetCapabilityByCategory` 或完整视频 codec/capability/format/surface 生命周期、JPEG/HEIF 图像编解码、HCodec/Codec Component HDI 信号可返回对应媒体候选；AVCodec、HCodec、Image Codec HDI 分别执行其专属 capability、buffer/format 与 software fallback 门禁。 |
| 负例 | 同步 IPC proxy、锁等待、泛化数组循环、无音效语义的 buffer、普通图像代码但无 codec 生命周期，均返回 0 条不相干 GPU/DSP/媒体候选。 |
| 不允许的结果 | 把 Vulkan/OpenGL header、系统 GPU 路径、HDI/VDI/Codec HDI/Image Codec HDI 合同、AVCodec 头文件、麒麟/鸿蒙画像写成当前设备可用、应用可直接调用或已有性能收益。 |

对应 regression 必须覆盖 IPC 与 `std::vector::resize` 零候选、RGBA 亮度优先 GPU、PCM effect 命中 DSP、AVCodec capability、JPEG/HEIF 与 HCodec Component HDI 各自命中媒体记录、暴露层级保留和排序稳定性。调整关键词、分数、单元范围或上下文截断时，必须更新版本化 manifest/hash 并同时跑完这些回归。

## 8.2 当前基线回归：先路由再检索，且路由必须可修订

`hardware-unit-routing` 曾是 `strategy-hardware-kb` v1.7.0 的确定性前置 Skill。当前生产链使用模型优先的 `hardware-semantic-routing`：模型基于可见代码、硬件中立画像和能力卡选择候选目录；确定性路由只可显式用于离线审计基线，不能进入生产 context 或模型 Prompt。路由只决定本轮优先检索范围，不是不可逆事实；策略精核或真机证据可以带原因触发重新路由，同一单元内也可以重选 API。

| 项目 | 固定预期 |
| --- | --- |
| 输入 | 可见代码、函数/事件名、版本化 `hardware_unit` 描述和受限静态代码信号；不读取用户画像作为 capability，且不把 `source_capability_record` 当完整 API 合同。 |
| 正例 | 三层矩阵乘加路由到 NPU/NNRT；RGBA 亮度路由到 GPU；PCM effect 路由到 DSP；JPEG 编码路由到媒体 image codec。随后只从对应目录取默认 Top-2 API。 |
| 负例 | IPC、锁、I/O、递推 prefix scan、普通 container/buffer 和没有 codec 生命周期的泛化图像代码不路由到任何单元。 |
| 隔离 | 一次 context 只有一条主单元硬件描述和该目录 API Top-N；GPU 成功路由不得附带 NPU/DSP/媒体 API，反之亦然。 |
| 不允许的结果 | 把路由分数、硬件目录存在、用户选择的麒麟/鸿蒙画像或 API header 写成目标设备已支持、应用可以直接调用或一定有收益。 |
| 可逆性 | 新路由必须生成新 artifact，并使旧路由之前产生的策略结果退出当前态；旧结果、失败 API 和真机证据仍作为历史保留。 |

改动路由 Skill、能力卡、默认 Top-N、回退语义或紧凑 context 截断时，必须运行语义路由泛化集、API retriever 回归、60 条 preflight/self-check 和完整前后端构建；不得只凭单次模型回答调整。

## 8.3 当前基线回归：自建 API 知识库必须以合同为主体

`custom-accelerator-api-kb-builder` 是模型编排 Skill 与确定性 `bounded_api_contract_discovery/v2` 的组合。Agent 负责核对 Connector、调用工具、检查质量和解释边界；后端扫描器负责候选生成、暴露层分级、函数/算子平衡 Top-N 与质量计数。模型无法通过更长提示词修复扫描器已经丢失或错误排序的候选，因此两个层次必须独立测试、协同发布。

| 项目 | 固定预期 |
|---|---|
| API 主体 | 公开 SDK/NDK、模块导出头文件、HDI/VDI/IDL 合同和算子/能力枚举优先；内部实现只作补充证据。 |
| 类别平衡 | 大型枚举不能挤掉设备、建模、编译和执行函数；大量普通函数也不能让算子/格式常量消失。 |
| 干扰负例 | `Service`、`Manager`、`Builder`、CPU 示例、test/mock/fuzz 中的方法不能因 `model`、`tensor`、`buffer` 等宽泛词项压过公开合同。 |
| 质量语义 | JSON 落盘和 `quality.status=pass` 是两个状态；无公开合同或内部实现占主导时必须为 `warning`。 |
| 泛化边界 | 单仓符号可以出现在 evaluator oracle 中，不能写入生产筛选分支。修复必须基于暴露层、声明类型和证据角色等通用特征。 |
| Agent 表述 | Agent 必须读取公开/契约、算子、内部实现计数；`warning` 只能报告“资产已生成、质量待复核”。 |

以下变更视为同一协同版本面：单元别名、路径层级、函数/枚举/宏解析、排序分数、Top-N 配额、记录上限、质量阈值、Skill 文本、MCP 工具说明、Action 摘要和 UI 状态。任一项变化都必须提高相应实现或 Skill 版本，运行 `test_custom_api_kb`、Runtime Action 回归、完整后端测试和前端 lint/typecheck/build；拥有代表性源码仓时还要运行 shadow scan，比较关键 API 族与内部实现占比。

OpenHarmony NNRT 快照回归是当前代表性真实仓检查：公开 `OH_NN*` 函数和 `OH_NN_OPS_*` 算子必须同时可见，`PreparedModelService` CPU 示例不能成为主记录。该符号只定义测试期望，不构成生产硬编码。完整计数、快照和允许变化范围见 [自建异构单元 API 知识库 Spec](custom_accelerator_api_knowledge_base_spec.md)。

## 9. 版本与发布

- 行为、结论门禁、输出 schema 或 context 投影发生变化时，必须提高相应 Skill 的实现版本或 prompt 版本，并在 `evolution` 记录中关联 regression ID。
- 生产运行使用 source-controlled Skill profile 冻结一组协同版本。已发布 profile 不得原地修改；新版本须创建新 profile、记录父版本和内容 SHA，并保留可立即选择的稳定回滚目标。
- 分析记录和 Agent Run 固化当时的 profile ID、profile SHA 与 Skill 版本；切换 profile 只影响后续请求，不得改写历史 artifact 或在运行中替换 prompt。
- 新增模型文本、修改检索排序或阈值必须经过 source review；发布前运行 registry 校验、后端回归、前端 lint/build 和相关冻结 benchmark。
- 出现证据越界、未声明自动升级、action 越权或密钥泄露风险时，停止发布并回滚到上一个已验证版本。
- 独立盲推理与流程结果对照可用于发现流程增益、过度约束或遗漏，但同模型对照 verdict 不是准确率 oracle，不能自动选择、训练或发布 Skill。
- Phase A 不做在线 A/B 自动择优，也不将用户工作台中的单次模型回答用于自动训练或自动发布。

## 10. Phase A 已实现与后续工作

当前实现已经提供：运行时 Skill registry、可切换且带内容 SHA 的 Skill profile 版本库、模型 prompt 版本、独立盲推理 Session 与流程结果对照、可逆路由/API 重选、`reasoning_inference_transcript` 与 Agent JSONL 执行 Trace 审计、direct IPC 校准门禁、对应单元回归，以及真机 `experiment_learning_report/v1` 候选记录。它们构成从真实实验观察到受控改进候选的最小闭环。

后续可增加候选归类与晋升界面、独立的 `skill_feedback` artifact、回归样本浏览器、跨设备复现矩阵、shadow-run 对比报表和版本化 evaluator 服务；这些能力必须保持只读审计与人工发布，不得让模型直接修改生产 Skill。
