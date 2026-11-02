# 实验流程 Playground

## 目的

机会报告下方提供两条彼此隔离的实验流程分析路径。它们保留“用户描述流程、Agent 理解代码、页面展示 Mermaid”的核心体验，但不使用 `application-flow-scenario/v2`、阶段编译器或正式应用流程 Skill。

| 页面 | 运行时输入 | 时间口径 | Skill |
| --- | --- | --- | --- |
| Playground | HiPerf 原始数据或报告 | 函数 Self 百分比、样本数，或有采样周期依据的估算时间 | `experimental-flow-hiperf-reconstruction` |
| Playground 2 | `.htrace`、Trace JSON 或文本报告 | event wall/span/inclusive duration；只有输入提供采样或调度归因时才使用 CPU self time | `experimental-flow-trace-reconstruction` |

## 执行链

1. 页面选择一个主代码仓和最多 11 个关联代码仓，可分别限定相对目录；随后输入完整用户流程并上传对应运行数据。
2. 页面把任务提交给当前页面 Agent Session，并按分析类型只加载一个实验 Skill。
3. Agent 使用原生代码理解能力探索源码，通过 `experimental_flow_read_evidence` 读取平台托管的运行数据。
4. Agent 自主选择 Mermaid 图型和分析方法，以真实函数、调用边界和运行指标生成细粒度流程图。
5. `experimental_flow_publish_result` 保存自由 Mermaid、Markdown 说明、全部源码范围、证据绑定和父版本；页面收到工具结果后立即刷新。

## 隔离边界

- 两种分析使用不同的 `analysis_kind`、任务类型、Skill 和历史目录，证据类型不交叉引用。
- 平台只校验代码仓/运行数据绑定、父版本血缘、字段大小和 Mermaid 浏览器安全。
- 实验结果存储为 `experimental_flow_result/v2` 轻量信封；信封不是流程结构 Schema，Mermaid 内部结构由 Agent 决定。
- 实验工具不会写入应用流程 discovery、scenario、Trace 校准或机会推理工件。

## 运行数据

上传文件存放在平台托管的 `raw-inputs` 目录，并登记为 `experimental_flow_evidence` 工件。MCP 读取工具返回真实服务端路径、文件元数据和可用的文本预览；二进制 `perf.data` 或 `.htrace` 由 Agent 使用部署环境中可用的分析工具读取。

## 结果版本

“新建分析”创建独立结果；历史结果中的“继续修改”携带 `parent_result_artifact_id` 创建下一版本。历史 Mermaid 保持不可变，删除操作使用逻辑墓碑，不改写其他分析记录。
