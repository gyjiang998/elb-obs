# 异构优化使能平台架构图集

> 用途：辅助阅读 Trace 热点分析、源码与 AST、知识库、统一 Agent Session、Build Worker、HDC Device Bridge、真机验证和 Skill 工程设计。图片是概念与评审视图；接口字段、状态机、证据等级和安全门禁仍以各权威 Spec 的文字契约及实际工具结果为准。

关联规范：[平台 Agent Runtime、后端编译与 HDC Device Bridge Spec](platform_agent_runtime_build_device_bridge_spec.md)、[Agent-Native Web 平台 Spec](heterogeneous_enablement_web_platform_spec.md)、[异构优化机会平台 Spec](heterogeneous_optimization_opportunity_platform_spec.md)、[Skill 演进与评测 Spec](skill_evolution_and_evaluation_spec.md)。

## 1. 端到端总体架构

![异构优化机会平台端到端总体架构](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/heterogeneous_platform/01_platform_end_to_end_architecture.png?raw=1)

主链路为：Trace 或代码输入 -> 热点发现 -> 事件归因 -> 源码定位 -> AST 与代码特征 -> 硬件路由 -> 主单元 API 匹配 -> 机会推理 -> 可选真机验证 -> 机会报告。图中同时区分两个事实面：`Kimi Session` 保存对话语境和自主推理过程，`Platform Run` 保存 artifact、工具结果、审计和恢复事实。

## 2. 统一 Kimi Session 与 Agent Runtime

![统一 Kimi Session 与 Agent Runtime](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/heterogeneous_platform/02_unified_kimi_session_runtime.png?raw=1)

同一个 `analysis_id` 绑定同一个 workspace、Kimi Session 和 Platform Run。硬件路由、硬件机会推理、真机验证及页面助手对话都按时间顺序追加到该 Session；阶段切换只装配精简上下文，不重复发送完整上阶段输入输出，也不另建互不知情的会话。

Kimi 原生工具承担后端源码分析、文件、Shell、编译和同机 HDC；Per-Run MCP 只承担浏览器页面、平台工件和 Windows HDC 跨边界动作。浏览器以 SSE 展示思考、回答和工具状态；用户停止会同时传播到会话与仍在运行的 Build/Device Job，但不会删除已经发生的事实。

## 3. Trace 热点发现与源码归因

![Trace 热点发现与源码归因](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/heterogeneous_platform/03_trace_hotspot_source_attribution.png?raw=1)

原始 `.htrace` 与 Trace 团队 JSON 报告是两种可独立使用的输入。热点发现同时使用事件 self-time、嵌套总时长、频次、短窗口聚集、关键路径状态和 wakeup 链聚类；结果按模块组织，每个模块展示 Top-N，避免系统噪声淹没业务热点。

源码归因先解析 event 名称和 HiTrace 模块标签，再搜索精确插桩点并绑定源码快照。候选范围从插桩语句块扩展到所在函数，只有在语义需要时才展开被调函数；这样可避免将包含多段逻辑的整个函数误判为热点实现。

## 4. AST、代码特征与异构机会识别

![AST、代码特征与异构机会识别](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/heterogeneous_platform/04_ast_feature_hardware_matching.png?raw=1)

函数级 AST 必须绑定真实编译环境与源码主体。特征画像覆盖数据类型和形状、访存模式、计算密度、数学算子、迭代依赖、归约、分支、锁/IPC/I/O、数据生命周期和任务粒度，并区分“已观察”“推断”“缺失”三种证据状态。词法静态扫描可帮助确定候选范围，但不能伪装成完整 AST。

硬件路由由模型基于代码、特征画像和硬件能力简卡进行语义判断，规则只作为参考证据与审计基线。确定主硬件后，仅检索该单元的 API Top-N，再对输入输出、格式、生命周期和运行时核验条件做精确比较。图中的 GPU API 是候选语义示例，不表示任意设备已支持。

## 5. 异构优化知识库体系

![异构优化知识库体系](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/heterogeneous_platform/05_knowledge_base_system.png?raw=1)

三类知识保持独立：优化知识库只收录严格异构实现与 CPU/software fallback 案例；硬件知识库描述单元能力、限制和运行时查询方式；加速器 API 库按 NPU、GPU、DSP、媒体与图像编解码单元保存接口摘要、详细语义、数据约束和验证前置条件。

离线构建必须分析真实 CPU 实现主体，而不是把函数名当作特征。每条记录携带源码快照、路径和行号、内容哈希、提取器版本及证据等级；候选不会自动进入正式库。三个知识库分开保存、独立版本化，并通过稳定 API 提供检索。

## 6. Agent 驱动的真机验证闭环

![Agent 驱动的真机验证闭环](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/heterogeneous_platform/06_real_device_validation_loop.png?raw=1)

真机验证不是固定三轮模板。Agent 在同一 Session 内提出最小可证伪实验，先查询真实构建 Profile，再生成源码与 Shader、编译、部署、执行、比对、诊断并迭代，直到获得通过证据、遇到真实外部阻塞或用户停止。

Kimi 可直接使用后端源码、Shell、编译器和同机 HDC；需要可复现实验证据时再进入固定 SDK/Profile 的 Build Worker，硬件知识库不能充当头文件目录。Windows 设备操作经 Device Gateway 和 HDC Bridge 执行，Bridge 不暴露 Windows 通用 Shell。完成门禁要求真实构建、部署、执行结果，并分别陈述功能等价、API 可用、热点集成、性能收益和功耗收益。

## 7. Skill 工程与持续评测闭环

![Skill 工程与持续评测闭环](https://github.com/gyjiang998/heterogeneous-seek/blob/docs-assets/docs/images/heterogeneous_platform/07_skill_engineering_evaluation_loop.png?raw=1)

Skill 由目标边界、领域背景、通用判据、检索策略、工具方法、输出契约、停止条件、反证和版本信息组成。上下文只保留当前任务所需的热点代码、已观察特征、上阶段结构化结论、主硬件 API Top-N 和必要证据，过滤重复 artifact 元数据与无关历史，以降低 token 消耗并保持稳定前缀。

测试矩阵覆盖 GPU、NPU、DSP、媒体和 CPU 并行的正样本、负样本与边界样本，并加入函数改名、变量改名、代码重排和额外外围代码等变换。Skill 修改必须经过 holdout 与全量回归；局部指标提升不能以牺牲跨模块泛化为代价。

## 图的使用约束

- **展示层**：适合设计评审、项目介绍、技术汇报和 Spec 导航。
- **实现层**：具体 action、artifact schema、恢复语义、权限和部署参数必须回到权威 Spec 与代码核对。
- **证据层**：图中的绿色标识表示该类证据可被记录，不代表任意代码、设备、API 或性能收益已经验证。
- **API 名称**：图中 API 名称用于解释检索层级；设备可用性以 capability query、真实构建和真机执行结果为准。
