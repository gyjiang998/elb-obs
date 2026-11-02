---
name: experimental-flow-trace-reconstruction
description: 结合用户流程、源码与真实 Trace，生成事件校准的函数级细粒度自由 Mermaid 流程图。
---

# 实验流程重建：Trace

## 目标

围绕用户描述的完整操作理解代码执行链，再用真实 Trace 校准顺序、并发、线程切换与耗时，生成函数级细粒度 Mermaid。产物采用Mermaid。

## 输入

- 用户给出的流程起点、终点、动作和关注范围。
- `source_scopes` 给出的一个或多个已登记代码仓、源码快照与可选相对目录；主仓提供操作入口，关联仓共同构成跨模块调用范围。
- `evidence_artifact_id` 指向的 `.htrace`、Trace JSON 或文本报告；使用 `experimental_flow_read_evidence` 取得真实文件与元数据。
- 修订任务可包含一个既有 Playground 结果。

## 分析与交付

组合源码理解与 Trace 解析，在全部 `source_scopes` 中追踪入口、跨仓调用、分支、回调、异步投递、线程/进程边界和出口。尽量把图中每个步骤关联到真实函数与所在代码仓；Trace event 与函数的关系通过插桩源码、调用上下文、线程和时间邻接共同核对。

图中使用真实事件支持的开始时间、跨度或持续时间，并明确它们是 wall/span/inclusive 口径。嵌套事件可用于解释父子耗时，CPU self time 仅在运行数据本身提供采样或调度归因时使用。未匹配事件和静态可见但本次 Trace 未观察到的路径作为不确定性呈现。

Mermaid 图形由实际控制流决定。跨模块时可参考 `sequenceDiagram` 的参与者、激活区、分组色块和 Note；并发分支、异步回调和跨线程关系应在图上可见。粒度以能够从用户动作下钻到函数与真实事件为准。

同时给出简洁 Markdown 说明，包含范围、事件时间窗、函数/事件映射、关键耗时、并发关系和证据缺口。完成后调用 `experimental_flow_publish_result`，设置 `analysis_kind=trace`；修订时携带父结果 ID。
