# Documentation

- [总体技术文档](heterogeneous_optimization_enablement_platform_technical_overview.md)
- [DPVS / CVS 线性扩展性能分析台](dpvs_cvs_performance_analysis.md)
- [桌面工作台设计与平台架构复核](desktop_workbench_design.md)
- [关键技术](heterogeneous_enablement_key_technologies.md)
- [工程问题复盘与经验总结](heterogeneous_enablement_platform_engineering_retrospective.md)
- [函数代码画像技术说明](function_code_profile_technology.md)
- [实验流程 Playground](experimental_flow_playgrounds.md)
- [Spec 索引](spec_index.md)
- [文档与实现同步规范](documentation_change_control.md)
- [Web 平台 Spec](heterogeneous_enablement_web_platform_spec.md)
- [Agent Runtime、Build Worker 与 Device Bridge Spec](platform_agent_runtime_build_device_bridge_spec.md)
- [场景采集与并行建模](scenario_capture_parallel_modeling_spec.md)
- [负载特征](workload_features_model.md)
- [Trace 管理与寻优校准](trace_storage_and_calibration.md)
- [离线校准模型包](offline_calibration_packages.md)
- [Trace 分析技术 Spec](trace_analysis_technology_spec.md)
- [真机验证 Spec](real_device_validation_spec.md)
- [Skill 演进与评测 Spec](skill_evolution_and_evaluation_spec.md)
- [执行经验提炼与 Skill 自适应演进 Spec](execution_experience_evolution_spec.md)
- [自建异构单元 API 知识库 Spec](custom_accelerator_api_knowledge_base_spec.md)
- [严格异构 CPU fallback 案例总览](strict_heterogeneous_cpu_fallback_overview.md)

文档中指向 OpenHarmony 源码的链接是知识证据定位，不表示完整源码随平台仓库分发。在线知识资产位于 [`data/knowledge-bases/`](../data/knowledge-bases/)，用户源码和 Trace 通过运行时连接器接入。

部分研究期 Spec 仍记录 `docs_ffrt/tools/`、Xicen 或外部评测流水线，用于解释知识资产的生成和演进历史；这些离线工具与历史中间产物不属于当前部署闭包。仓库内可直接运行的工具统一位于 [`tools/`](../tools/) 和 [`backend/tools/`](../backend/tools/)。
