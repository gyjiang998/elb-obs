# 异构优化使能 Specs

总体技术入口：[heterogeneous_optimization_enablement_platform_technical_overview.md](heterogeneous_optimization_enablement_platform_technical_overview.md)。它从技术体系和工程资产角度统一说明知识库、代码智能、Skill 工程、Agent Runtime、专用 Harness、真机证据闭环及模块级代码知识库路线；本文下列专项 Spec 继续作为设计和代码实现的权威依据。

共同规范附件：[cross_spec_contract.md](cross_spec_contract.md)。它定义 artifact、源码绑定、状态机、Knowledge Case、Git connector 与 action/skill 的统一契约。

代码、Skill、知识库、MCP/Action、页面和部署修改时必须遵循 [documentation_change_control.md](documentation_change_control.md) 的影响矩阵与合并前检查，避免实现、Spec、测试和部署说明再次漂移。

Skill 演进、回归和发布治理见 [skill_evolution_and_evaluation_spec.md](skill_evolution_and_evaluation_spec.md)。运行时 Skill 版本库、Agent-Centric 精简 profile、完整替换/兼容追加语义、独立盲推理对照、可逆路由和分级门禁见 [adaptive_reasoning_and_skill_version_spec.md](adaptive_reasoning_and_skill_version_spec.md)。

跨模型、Agent 框架、宿主、SDK 和设备的历史踩坑提炼、用户处置与 Skill 自适应闭环见 [execution_experience_evolution_spec.md](execution_experience_evolution_spec.md)。

| 权威 Spec | 职责 | 不负责 |
|---|---|---|
| [trace_analysis_technology_spec.md](trace_analysis_technology_spec.md) | trace 规范化、wakeup 链、trace 簇、时间局部性热点与关键路径证据 | KB、AST、网页和设备采集 |
| [heterogeneous_optimization_opportunity_platform_spec.md](heterogeneous_optimization_opportunity_platform_spec.md) | 异构 KB、源码静态特征、匹配引擎、优化空间、硬化分析与独立 benchmark 评测 | trace 解析技术、网页交互 |
| [heterogeneous_enablement_web_platform_spec.md](heterogeneous_enablement_web_platform_spec.md) | 单用户 Agent-Native 工作台、Linux 后端/Windows 浏览器拓扑、Kimi 原生后端 Coding Agent、代码仓/场景管理、API、skills 与页面动作 | 多用户隔离、KB 审核发布或把未审计实验写成 device benchmark 结论 |
| [multi_strategy_hotspot_opportunity_reasoning_spec.md](multi_strategy_hotspot_opportunity_reasoning_spec.md) | 热点代码的 KB-RAG、硬件能力上下文与 Direct LLM 三策略推理，包含用户选择的目标设备/系统画像和完整模型 transcript 审计 | trace 采集、设备 benchmark、把模型猜测升级为硬件事实 |
| [real_device_validation_spec.md](real_device_validation_spec.md) | Kimi 原生后端编译/本机 HDC、可选固定 Harness、Windows HDC Bridge、多轮实验与证据校准 | Windows 通用远程 Shell、外部搜索/抓取、把功能通过写成热点收益或多用户授权 |
| [platform_agent_runtime_build_device_bridge_spec.md](platform_agent_runtime_build_device_bridge_spec.md) | 可替换 AgentEngine SPI、Kimi 原生后端工具、持久化 Platform Run、可选 Build Worker、Windows HDC Device Bridge、跨主机恢复和统一可观测性 | 真机实验的机会判据、功能/收益证据解释或 Windows 客户端通用远程 Shell |
| [desktop_workbench_design.md](desktop_workbench_design.md) | `Desktop-first 0.1.93`：私有直连设备同时核验 TraceFS 与 HiTrace 服务态，服务残留以有界 `finish_nodump` 恢复；Trace 冲刷等待按缓冲区分级，PMU 未启动与采样器缺失分开诊断；同时继续提供 100 ms Agent 提交、失败自动重试入口、冻结寻优报告、有界 Agent 自动恢复、负载模板渐进展开、统一错误诊断、初始化快路径、分层工具计时、采样计划差量更新、原生 MCP、自定义 Agent、Monaco 双工作区和并行建模闭环 | 自动恢复、手机授权和服务器授权默认关闭；目标变化、授权撤销、超时、无法修复或再次失败都会终止恢复。Windows 负责设备事实与本地工作区；Linux 收敛为源码、代码分析、Trace 解析和异构案例编译。Bridge/共享设备不会强制停止来源不明的全局 Trace；设备进程只按 PID/starttime/boot/token 精确回收；Session 文本与工具结果限长脱敏，thinking 原文不读取；动态 Broker 凭据只从父环境继承 |
| [agent_framework_adapter_guide.md](agent_framework_adapter_guide.md) | `Implemented v1.0 / 页面提交合同 v1.4`：厂商中立 CLI Adapter、统一 PTY、有界粘贴等待与单次默认提交、传输诊断、可移植 Skill/MCP 工作区、浏览器模型临时注入及 AgentEngine 扩展合同 | 让新 Agent 框架先以真实 WebShell 接入，再按需实现结构化 Engine；不以页面 Agent loop 限制框架原生能力 |
| [skill_evolution_and_evaluation_spec.md](skill_evolution_and_evaluation_spec.md) | 确定性 Skill、模型 Skill 与页面 Agent 的版本、证据门禁、回归、上下文效率和发布治理 | 替代异构机会的独立 oracle/benchmark 评测 |
| [adaptive_reasoning_and_skill_version_spec.md](adaptive_reasoning_and_skill_version_spec.md) | 可回滚 Skill profile、版本化精简材料、隔离盲推理、流程结果对照、重新路由、API 重选和分级门禁 | 把同模型自评当作真值、用盲推理替代 KB/Harness/正式证据链、用短 overlay 冒充真实瘦身 |
| [execution_experience_evolution_spec.md](execution_experience_evolution_spec.md) | 历史 Agent/页面/真机执行源的证据化踩坑提炼、环境差异矩阵、稳定问题类、用户处置和 Skill 自适应演进 | 根据单次失败自动修改环境、代码、知识库或生产 Skill，或用当前机器倒填历史环境 |
| [hardware_api_template_library_spec.md](hardware_api_template_library_spec.md) | 204 条硬件 API 的语义族模板覆盖、Harness/Adapter/recipe 分层、GPU 成本感知选型和发布门禁 | 把模板历史 PASS 当作当前设备、热点集成或性能收益证据 |
| [hardware_kb_context_strategy_evaluation_spec.md](hardware_kb_context_strategy_evaluation_spec.md) | Hardware-KB 策略的 60 条分层 oracle 评测、模型本地执行、泛化指标和变更控制 | 声称真实设备支持、替代严格 source-backed 独立 benchmark |
| [custom_accelerator_api_knowledge_base_spec.md](custom_accelerator_api_knowledge_base_spec.md) | 从已登记服务端代码仓构建隔离、可删除的异构单元 API 自建库；定义公开合同优先、函数/算子平衡、质量状态、`custom:<kb_id>` 动态路由、单库 API Top-N 和回归门禁 | 自动并入平台发布库、把内部实现冒充公开 API、把落盘或词法候选写成设备支持或性能收益 |
| [application_execution_flow_reconstruction_spec.md](application_execution_flow_reconstruction_spec.md) | 为选定模块构建快照绑定底图，围绕用户目标有界检索并复核源码，经确认后生成场景 v2 有序 Stage JSON/Mermaid，并按需增强源码 HiTrace start/end | 把静态调用候选冒充运行路径、由 Agent 默选含糊流程、把函数名冒充 event，或将研究 DAG 字段混入既有场景 Schema |
| [code_knowledge_base_spec.md](code_knowledge_base_spec.md) | 模块级 Tree-sitter/SQLite 底图、跨页面公共检索 Skill、Agent 源码复核、估时/中止和版本化 Markdown/Mermaid 报告 | 全仓无界扫描、整图进入模型上下文、把静态关系写成运行路径，或把 Agent 架构解释当作源码事实 |

`algorithm_signature_based_heterogeneous_opportunity_detection.md` 是算法签名和 AST 方法论/实现参考，不是权威产品 Spec。Trace 相关规则以 Trace 分析技术 Spec 为准。

## 场景采集与并行建模

- [场景采集、持久化任务及建模接口](scenario_capture_parallel_modeling_spec.md)：自动发现本机/Windows Bridge、目标设备动态屏幕尺寸、HiTrace argv、10 μs 可选目标及 PMU 能力检查；runner 1.2.15 / screen_preparation v7，所有场景共用采集前屏幕准备，私有直连设备有界恢复 HiTrace 服务态，Trace 冲刷等待按缓冲区分级，PMU 未启动与采样器缺失分开诊断；设备采集器有界收尾且失败时回传最后清理阶段；优先尝试目标应用 aa start，含状态控制的 Power、动态手势及 USB 关闭；私有直连设备在新采样前安全收尾残留 Trace，Bridge/共享设备保持严格归属保护；临时延长熄屏时间并恢复，脚本传完后二次核验；准备无累计时间上限，单次 I/O 超时与取消、独立 20 秒失败取证，Bridge 现行为 1.4.9，实时结果与历史回放隔离；另含 Windows 本地采集包、Python 自动化、任务和 Agent 工具合同。
- [BuildItem 并行模型 v1](parallel_modeling_model_v1.md)：数据来源、关键路径、成本模型、核频搜索、结果与校准覆盖。
- [负载特征模型](workload_features_model.md)：BuildItem 阶段的计算、访存、调度 / 服务指标、测量来源、候选分型和特征版本绑定。
- [双任务负载组合干扰校准](parallel_pairwise_interference_v1.md)：定向单跑 / 共跑实验、训练与留出验证、事件驱动模型消费及未测适用域回退。
- [Trace 管理与寻优校准](trace_storage_and_calibration.md)：Windows 持久原始文件、平台副本管理、OPP 解释、离线微基准拟合及模型版本。
- [离线校准模型包](offline_calibration_packages.md)：硬件画像、冻结方案、真机拟合和发布版本的内容寻址分发、完整性校验、幂等安装与应用验证边界。

## 当前实现与审计资产

以下文档和数据是上述 Spec 的当前实现基线；它们提供已落地能力和证据状态，但不替代 Spec 中的 release、硬件能力或收益验收门禁。

| 资产 | 当前状态 | 用途 |
|---|---|---|
| [heterogeneous_optimization_enablement_platform_technical_overview.md](heterogeneous_optimization_enablement_platform_technical_overview.md) | 总体技术基线 v1.8（2026-10-31）：采集/特征/校准/建模主链路、Web/Bridge 与 Desktop-first 0.1.93 执行拓扑、HiTrace 服务态恢复、分级 Trace 冲刷、准确 PMU 未启动诊断，以及既有 Agent/SSH/工作区/知识库/真机证据闭环 | 面向技术负责人和工程团队建立统一系统认知，并导航到各权威专项 Spec |
| [agent_harness_engineering_insight.md](agent_harness_engineering_insight.md) | 基于当前实现与行业基线的 Agent Harness 工程审阅：4 项优势、4 项改进优先级、验收指标及 8 张评审图 | 不替代权威 Spec、模型效果 benchmark 或真机收益证明 |
| [heterogeneous_enablement_platform_engineering_retrospective.md](heterogeneous_enablement_platform_engineering_retrospective.md) | 历史 76 个工程问题族（统计截至 2026-08-26）；2026-10-27 追加设备选择、心跳、历史 ACK 阻塞与部署拓扑专项复盘，区分已修复症状和后续验证 | 不保存原始会话、密钥或设备标识，也不把静态/编译通过写成设备性能收益 |
| [adaptive_reasoning_and_skill_version_spec.md](adaptive_reasoning_and_skill_version_spec.md) | `agent-centric-page-2026-08-20` 为推荐运行时基线；每个页面绑定一个持久 Session，分析记录仅切换审计上下文，同时跳过已知 action 的全目录加载并强化结构契约 | 诊断流程资产对模型的增益或干扰，以更小上下文保留 Agent 自主性，并为 Skill 退化提供快速回滚 |
| [strict_heterogeneous_cpu_fallback_overview.md](strict_heterogeneous_cpu_fallback_overview.md) | 40 条用户态/三方库严格 source-confirmed pair；29 个唯一 CPU 函数定义已提取 Tree-sitter AST，11 条动态 provider/factory/外部实现保持 `not_ready`，计算核心匹配须经独立门禁 | 异构单元、CPU/software fallback、AST 特征与可匹配性总览 |
| [heterogeneous_enablement_key_technologies.md](heterogeneous_enablement_key_technologies.md) | 当前平台关键技术的 What / Why / How 说明，区分已落地能力、证据边界与后续设备验证闭环；含模型优先硬件语义路由、规则审计基线与变换泛化评测 | Trace、静态分析、知识库、API 检索、Skill、Agent、模型上下文和评测技术的整体工程视图 |
| [function_code_profile_technology.md](function_code_profile_technology.md) | 函数代码画像技术基线 v1.0：编译上下文、Clang AST/CFG、七类静态特征、证据等级、机器结构及 FP32 MatMul 完整可复现实例 | 将热点源码转换为可审计的类型、计算、访存、控制、依赖、运行时边界和并行条件；不替代 Trace 热度或设备收益证据 |
| [application_execution_flow_reconstruction_spec.md](application_execution_flow_reconstruction_spec.md)、`agent-resources/skills/application-execution-flow-reconstruction/` | `Implemented v2.0`：完整用户操作候选与交互修订、分层 Mermaid 到现有 Stage JSON 的确定性投影、多轮发现/场景版本目录、父子血缘、逻辑删除及同一页面 Agent Session 续改 | 面向任意业务流程生成可交互校准、可回溯演进的完整旅程和源码定位合同；完整并行 DAG、动态发生、时序和关键路径仍是独立能力 |
| [code_knowledge_base_spec.md](code_knowledge_base_spec.md)、`agent-resources/skill-library/skills/code-knowledge-analysis/`、`agent-resources/skill-library/skills/code-context-retrieval/` | `Implemented v1.6`：异步规模预估、可重复构建及血缘、构建/报告目录、报告子版本续改、逻辑删除、公共限幅检索与 Agent 源码复核 | 面向模块形成可恢复、可复用、可追溯且可管理的代码理解底座；静态候选、源码事实、Agent 归纳和运行时证据分层表达 |
| [semantic_stage_graph_and_trace_instantiated_workload_graph.md](semantic_stage_graph_and_trace_instantiated_workload_graph.md) | `Design Baseline v1.1`：交互驱动的分层语义阶段 DAG、证据驱动的大模型/静态工具协作机制，以及语义图与真实 Trace 结合形成实例化负载图和关键路径的完整技术设计 | 保留现有场景 v2 线性契约；新增派生图工件，不把静态可达、源码桩点或 span 包络误写成动态关键路径 |
| [real_device_validation_spec.md](real_device_validation_spec.md)、`demo/hardware-api-examples/device-run/` | v2.4.0 Agent-Native 真机实验闭环：完整实验源码工件、OpenHarmony NDK musl/领域 SDK 固定 profile、编译上传和 probe 绑定远端 Shell；GPU/NNRT 样例仅作为可参考的最小实验 | 将设备探测、实验源码、构建上传、设备执行、功能等价、热点集成与性能/收益边界拆开审计 |
| [platform_agent_runtime_build_device_bridge_spec.md](platform_agent_runtime_build_device_bridge_spec.md) | `Implemented M0-M3.5 + Kimi Engine v2.2.0`：AgentEngine SPI、Kimi 原生后端工具、页面/Windows HDC 跨边界 MCP、持久化 Platform Run、E0 Build Worker、Device Gateway 与 Windows HDC Bridge已落地 | M4 的模块级 harness、多用户认证/授权、企业密钥服务与产品级 benchmark |
| [platform_agent_runtime_architecture_figures.md](platform_agent_runtime_architecture_figures.md) | 7 张中文架构图：端到端平台、统一 Kimi Session、Trace 与源码归因、AST/特征匹配、三类知识库、真机验证闭环及 Skill 持续评测 | 不替代权威 Spec 的接口、状态机、证据与安全契约 |
| [优化知识库 README](knowledge/optimization/README.md) | 57 条正式优化知识记录：17 条规则/映射层 + 40 条严格用户态/三方库异构实现到 CPU/software fallback 案例 | 热点代码的优化模式、CPU AST profile 与 fallback 语义检索 |
| [硬件知识库 README](knowledge/hardware/README.md) | 22 条硬件单元记录，另含 NPU（146 条）、GPU（18 条）、DSP（9 条）和媒体加速（31 条）API 子知识库；媒体目录覆盖 AVCodec、HCodec、JPEG/HEIF 与 Image Codec HDI，全部要求设备侧证据 | 硬件接口、约束、关联案例、分单元 API Top-N 检索与设备能力门禁 |
| [自建异构单元 API 知识库 Spec](custom_accelerator_api_knowledge_base_spec.md) | `Implemented v2.1 / builder 1.1.0`：已登记 Connector、有界合同扫描、公开面分层、函数/算子平衡 Top-N、`pass|warning` 质量门禁、同名单元独立新建、`custom:<kb_id>` 路由/API 精核、hash 失效保护、用户删除和 Agent MCP 构建 | 用户私有或新异构单元的可审计 API 合同候选建库与机会推理；平台合并继续由开发者复核发布 |
| [NPU API 知识库](knowledge/hardware/NPU_API_KNOWLEDGE_BASE.md) | 由 OpenHarmony NNRT/MindSpore 源码生成的 NPU API 目录 | 每个公开入口/图算子的中文摘要、源码约束、AST/数据形态信号、严格 case 关联和运行时验证协议 |
| [GPU API 知识库](knowledge/hardware/GPU_API_KNOWLEDGE_BASE.md)、[DSP API 知识库](knowledge/hardware/DSP_API_KNOWLEDGE_BASE.md)、[媒体加速 API 知识库](knowledge/hardware/MEDIA_ACCELERATOR_API_KNOWLEDGE_BASE.md) | 由 OpenHarmony 源码生成的 GPU、DSP 与媒体加速 API 目录 | 区分公开 SDK、系统源码集成与 HDI/VDI 合同；记录图像/PCM/codec 静态信号、严格 case 关联和单元专属运行时门禁 |
| [hardware_kb_context_strategy_evaluation_spec.md](hardware_kb_context_strategy_evaluation_spec.md)、[tools/evaluation/hardware_kb_strategy_skill_cases.json](../data/evaluation/hardware_kb_strategy_skill_cases.json)、[tools/evaluation/hardware_semantic_routing_generalization_cases.json](../data/evaluation/hardware_semantic_routing_generalization_cases.json) | 60 条分层 Hardware-KB 策略 fixture + 17 条模型语义路由变换 fixture（公开输入 + evaluator-side oracle） | 验证 API/单元/CPU 并行/设备门禁，以及重命名、等价改写、语义缺失下的路由泛化，不进入 KB 或生产匹配 |
| [确定性模式层 README](../data/knowledge-bases/patterns/README.md) | 线上 AST matcher 所需的 7 个最小分层 JSON；不包含历史扫描批次、候选 JSONL 与人工审核中间产物 | 为模型策略提供可审计的结构匹配补充；正式页面案例仍以 40 条优化知识库记录为准 |
| [algorithm_signature_based_heterogeneous_opportunity_detection.md](algorithm_signature_based_heterogeneous_opportunity_detection.md) | Tree-sitter 函数级 profile 是当前可复现基线；Clang AST 仅用于高价值语义复核/后续增强 | 从热点源码特征到异构单元的匹配方法 |
| 历史 pair discovery 审计归档 | v21、v37、v38 的分批扫描、筛选和候选中间产物保留在离线研究归档，不随部署仓分发 | 需要重新构建或审计 KB 时由维护者单独挂载，不属于在线运行依赖 |

阅读顺序：先读总体技术文档建立全局认知；梳理设备链路问题或规划 Windows 本地执行时，先读桌面工作台设计，以及工程复盘第 10 节。随后再读共同契约、各权威 Spec 与 Skill 演进规范；需要比较“模型先验”和正式流程增益、切换 Skill 版本或理解流程回退时，再读自适应推理与 Skill 版本库 Spec。需要消费当前知识时先读优化知识库与硬件知识库 README，需要做 NPU 或 GPU/DSP/媒体 API 语义检索时再读对应 API 子知识库；需要从用户代码仓扩展新硬件 API 时，先读自建 API Spec 的合同优先与第 10 节回归门禁。需要从新代码范围提取启动、拍照、播放等跨模块业务流程时，读应用执行流程重建规范，并把生成的应用流程场景草案交给真实 Trace 校准。随后读严格 pair 总览；需要理解真机实验的证据语义时读真机验证规范，需要部署 Linux 后端、后端编译或 Windows 真机连接时再读 Agent Runtime 与 Device Bridge Spec。确定性模式层 README 用于说明在线 matcher 的最小输入；历史 pair discovery 审计由维护者在离线研究归档中保管。需要理解 AST、CFG、编译上下文和函数画像质量等级时先读函数代码画像技术说明，需要了解画像到异构案例匹配的算法时再读算法签名方法论。
