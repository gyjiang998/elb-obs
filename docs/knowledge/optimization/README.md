# 优化知识库

`optimization_knowledge_base.json` 是平台正式消费的**优化知识库**。它描述“什么样的 CPU/software 计算或控制模式可能对应已有异构优化路径”，不描述某台设备是否真的具备、启用或受益于某个硬件单元。

## 当前基线

| 项目 | 当前值 |
|---|---:|
| 总记录 | 57 |
| 结构化优化规则、映射、代价与热点规则 | 17 |
| 严格异构实现 + CPU/software fallback 案例 | 40 |
| 内核 device-offload pair | 0（明确排除） |

40 条严格案例来自三个彼此独立、可追溯的源码审计批次：

| 来源组 | 数量 | 覆盖范围 |
|---|---:|---|
| `v21_user_space` | 17 | Foundation、图形、多媒体、ArkUI 等用户态路径 |
| `v37_audio_dsp` | 1 | Audio Effect HDI DSP 到 AP 软件 effect chain |
| `v38_mindspore` | 22 | MindSpore Lite Kirin NPU/NNRT 到 CPU kernel 保留路径 |

每条 `strict_heterogeneous_case` 都包含异构执行锚点、CPU/software fallback、静态审查结论、源码引用、函数级 CPU AST profile 和统一的 `target_unit`。AST profile 的 `binding_status` 与 `compute_core.matching_status` 必须原样保留：已定位函数不等于已证明主计算核心，也不等于可直接作为热点匹配样本。

## CPU 代码特征摘要

40 条严格案例都附带 `cpu_code_feature_summary/v1`。这是从各案例保存的函数级 AST profile 派生的、用于热点代码匹配的证据层，不是由标题、硬件名称或大模型补写的描述。字段分为两类：

| 字段 | 匹配用途 | 证据边界 |
|---|---|---|
| `algorithm_tags`、`notable_callees`、`math_calls`、`operators` | 将热点函数的算法语义、调用边界和数学计算形态与已有 CPU fallback 交叉检索 | callee 只说明当前函数可见调用边界，不能替代被调函数的实际 CPU 负载。 |
| `data_type_evidence` | 比较源级可见的整数、浮点、对象/容器类型线索 | 未抽到类型时必须保留“未证明”，不能假设 FP32、INT8 或格式。 |
| `memory_access` | 比较指针表达式、数组下标和赋值节点的存在及计数 | 不从这些节点推断连续性、stride、对齐、buffer 生命周期或 DMA 可行性。 |
| `control_flow`、`parallelism` | 比较循环/分支规模、最大循环嵌套和 OpenMP pragma | 未观测到 pragma 不代表不能并行；循环在未解析 callee 内时也不会出现在外层函数。 |
| `binding_status`、`compute_core_status` | 决定案例能否作为函数结构样本，而不是只作 fallback 语义参考 | `runtime_selected_provider`、`not_ready` 等状态绝不升级为已验证的主计算核。 |

`availability=ast_unavailable` 的案例仍是严格确认的异构实现与 CPU/software fallback 关系，但没有唯一、可解析的 CPU 算法函数；它们只能用于分发/fallback 语义和硬件路径参考。优化知识库页面只展示 40 条严格**异构案例**。17 条通用优化规则仍保留在 artifact 中作为内部确定性匹配与 RAG 检索层，不以案例或候选形式呈现；`knowledge_case_catalog.json` 的候选也不在该页面展示。

## 边界

- `source_confirmed` 只表示源码中的异构路径与 CPU/software fallback 关系经过静态复核。
- case 是模式、代码结构和回退语义的证据，不能证明当前 trace 热点值得卸载。
- 本库不声明设备可用性、驱动/API 可用性、格式/shape 支持范围或性能、功耗收益；这些只能由硬件知识库与设备侧证据补足。
- `knowledge_case_catalog.json` 的候选发现仍独立保存，不得提升为 `strict_heterogeneous_case`。

## 构建与校验

从工作区根目录执行：

```bash
python3 docs_ffrt/tools/build_separated_knowledge_bases.py
python3 docs_ffrt/tools/build_separated_knowledge_bases.py --check
```

构建器会校验严格案例总数为 40、三个来源组分别为 17/1/22、每条案例均有 AST profile、没有 `kernel_` 模块泄漏，并为所有输入文件写入 SHA-256 provenance。输入审计链仍位于 [旧知识库构建输入目录](../heterogeneous_kb/README.md)。
