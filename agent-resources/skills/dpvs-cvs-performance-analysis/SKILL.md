---
name: dpvs-cvs-performance-analysis
description: "分析 CVS/DPVS 单节点与多节点扩展性能，归一跨平台 perf、sar 和 NUMA 材料，生成带来源、可比性和置信度的报告。适用于 1 节点与 16 节点 Guest 对比及 Host 侧佐证；不用于把缺失采样补成性能结论。"
---

# CVS / DPVS 性能对比分析

把已采集材料转成可追溯的对比结论，区分观测、计算、候选解释与验证结果。默认分析已有文件；任务包含采样或优化时再沿用户授权执行，不自动更改线上绑核、队列、同步或网卡配置。

## 输入与工具

- 优先使用任务已经提供的工件、分析器合同和角色映射。典型场景为 `baseline → single_node_guest`、`scaled → sixteen_node_guest`、`host_single_node → host_single_node`；角色与实际观测节点数分别记录，不能把 16 节点场景中一台 Guest 的文件当成 16 台总量。
- 先读 [输入与格式规则](references/input-contract.md)，登记每个文件的 SHA-256、角色、解析器和解析置信度。格式按内容识别；缺少文件仍可分析已有部分。
- 使用本轮实际可用的文件读取与分析工具，不猜测工具名、参数或平台 API。已有标准化结果时先核对来源和 coverage，再使用它。项目内脚本或远端工具以其当前帮助/合同为准。
- 未被解析的段落、无法读取的二进制、冲突的角色与时间单位作为显式缺口保留；不要静默跳过后宣称“已完整支持此平台”。

## 分析决策

1. 建立对比边界：节点数、实际采样节点、Guest/Host、线程/CPU 范围、负载、吞吐口径、版本、时间窗与采样配置。无法确认的项保持 `null/unknown`；先完成不依赖这些项的分析。
2. 对 perf、sar、NUMA 分别判断可比性，采用 [比较与诊断规则](references/comparison-rules.md)。展示原始值、可计算的归一值、变化量和计算前提。一个来源可成功解析，但仍不能与另一个来源作定量对比。
3. perf 同时保留样本数与占比；归一绝对样本需有时长及采样频率/period 策略，或可核验的同配置证据。采样事件、目标范围或硬件语义不同，不能只除以时长便宣布开销增长。不要混用 Self/Children、samples/event counts，或从周期加权占比反推精确样本数。
4. 先解释业务吞吐、PPS、每包成本和线程/队列分布，再定位热点。仅有单节点 Host 数据时，只能描述该基线的拓扑/分配状态，不能宣称它证明了 16 节点 Host 争用或 NUMA 退化。
5. 每个 finding 给出支持证据、替代解释、置信度及最小验证动作。没有运行证据的同步实现、virtio 后端、队列映射保持未知；源码事实与部署事实分开。

## 交付

使用 [报告模板](assets/report-template.md) 组织结果，按实际证据删减空表而保留缺口。需要 JSON 时可从 [分析骨架](assets/analysis-template.json) 开始；骨架不是已生成的结果，也不替代当前分析器的嵌套字段合同。

结果顶层使用：`schema_version`、`analysis_id`、`manifest`、`coverage`、`scaling_analysis`、`perf_comparison`、`sar_comparison`、`host_sar_observations`、`host_numa_analysis`、`findings`、`recommendations`、`limitations`、`provenance`。`host_sar_observations` 是 Host 单机的只读观测，不冒充 baseline/scaled Host 对比。保留现有工件 ID 和 schema 版本，不自行编造平台工件。文件清单提供到原文位置的引用，派生值提供公式与输入引用。报告首段直接回答能确认什么、还不能确认什么。

交付前核对：原始材料未改写；缺失项未变成零；占比用百分点和相对变化正确表达；全机 `all` 与 per-CPU 未重复汇总；NUMA 静态页分布未写成访问带宽；建议没有超过证据强度。跨平台或低覆盖输入仍可交付部分报告，但必须显式标记不可比项。
