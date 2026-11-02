# 异构优化使能跨 Spec 契约

> 状态：`Normative v1.1`
> 适用：Trace 分析技术 Spec、异构优化知识库与机会识别引擎 Spec、Agent-Native Web 平台 Spec、平台 Agent Runtime 与 Device Bridge Spec。  
> 定位：这是各权威产品 Spec 的共同规范附件，不单独定义一条业务流水线。

## 1. 共同原则

1. 所有跨模块数据均为不可变 artifact；业务对象只能引用 artifact hash，不能就地覆盖内容。
2. 模型、Agent 和 UI 只能提出或选择；热点、因果边、源码位置、KB 命中和机会结论必须由版本化确定性模块生成。
3. 一个 artifact 永远绑定一个 `project_id`、一个 `source_connector_id` 与一个 `source_binding`。
4. 未达到 `exact` 源码绑定、未选定 trace cluster、或依赖非 confirmed KB 的结果只能是 `development_candidate`，不得声称 production-ready。
5. 评测集、评测 oracle 与预测结果必须与 KB 构建输入隔离；评测只能度量 KB/识别器，不能反向自动提升 KB confidence 或写入知识层。
6. **操作完成状态与语义质量状态必须分离**：HTTP 2xx、artifact 已落盘或任务 `completed` 只表示执行链完成；凡是目录生成、检索、分析或模型推理结果，还必须用独立 `quality.status` 表示其内容是否达到该 artifact 类型的质量门禁。UI、Agent 和下游模块不得把“已生成”解释为“可用”或“高质量”。

## 2. Artifact Envelope

所有 JSON/JSONL artifact 的顶层或元数据对象必须包含：

```json
{
  "artifact_type": "trace_analysis_result",
  "schema_version": "1.0",
  "artifact_id": "art_01J...",
  "project_id": "local-default",
  "source_connector_id": "local-workspace",
  "source_binding": {
    "declared_revision": "device-build-or-git-id",
    "resolved_snapshot_id": "source_snapshot_xxx",
    "match_level": "exact",
    "resolver_version": "1.1",
    "snapshot_state": "dirty",
    "worktree_fingerprint_sha256": "<only when local edits are present>"
  },
  "producer": {"name": "trace-clustering", "version": "1.0.0"},
  "created_at": "2026-07-20T00:00:00Z",
  "content_sha256": "<canonical-content-sha256>",
  "input_artifacts": ["sha256:<input-hash>"]
}
```

`content_sha256` 对不含自身的 canonical JSON/JSONL 内容计算。时间、artifact ID 不参与缓存 key；输入 hash、producer version、参数 hash 和 source binding 参与。

## 3. 源码绑定

| `match_level` | 含义 | 可执行动作 |
|---|---|---|
| `exact` | trace 的声明 revision 与本地 source snapshot 完全相等；开发工作树存在本地修改时，还必须绑定其内容指纹；声明来源由输入或独立 binding artifact 追溯 | 在满足事件源码定位门禁后执行 AST、KB 匹配，最多生成 development candidate |
| `compatible` | 分支/版本相近但无法逐提交证明 | 仅预览、人工选择源码、显式风险标记 |
| `unknown` | 无法建立映射 | 保存 trace、展示 cluster，禁止自动源码/KB 匹配 |
| `mismatch` | 已知不一致 | 拒绝分析 run |

外部 trace 的 device build ID 与 Git snapshot 的映射必须是独立 `source_binding_map` artifact，含来源、规则、置信度和过期时间；不得只按字符串相似度判定 exact。对 raw `.htrace` 等未携带 revision 的输入，`trace_source_binding_attestation` 可在用户明确确认后，把 `unknown` trace 派生为绑定到当前 connector snapshot 的新 `trace_analysis_result`；本地开发工作树存在未提交修改时，v1.1 snapshot 将受影响文件内容纳入 snapshot ID，并在 binding 中记录聚合 worktree fingerprint。它必须记录原 analysis artifact、actor、原 binding、snapshot / worktree equality 复核和“平台无法独立证明设备构建来源”的事实。原 artifact 不得改写，`mismatch` 不得被 attestation 覆盖；Agent 还必须记录用于声明的场景证据。attestation 只允许 development 分析，不能替代可验证 device-build mapping 的 release 证据。

`exact` 不是一次性标记：在进入自动 AST/KB 分析前，平台必须重新计算当前 connector 的 source snapshot，并确认它仍等于 artifact 绑定的 snapshot；含本地修改的开发快照还必须复核 worktree content fingerprint。源码在绑定后发生变化时，以 `SOURCE_BINDING_MISMATCH` 拒绝 analysis run，要求刷新 snapshot 并重新建立绑定。源码特征 artifact 还必须记录实际文件 hash。发布/正式 KB 的 release 流程仍可单独要求 clean snapshot。

## 4. 统一状态机

```text
uploaded
  -> input_validated
  -> converted                 (only raw .htrace)
  -> normalized
  -> clustered                 (只有原始/导出 trace 需要)
  -> cluster_selected          (user, agent, or autonomous system)
  -> event_cataloged
  -> source_resolved
  -> feature_extracted
  -> kb_matched
  -> development_candidate
  -> exported
```

- 每一步产生新 artifact 和 `analysis_run_step`，不可改写上一步。
- 任一步可进入 `failed`、`cancelled` 或 `partial`；`partial` 必须列出可继续处理的 item 与失败 item。
- `production_ready` 不属于 Phase A 状态；只有后续严格 KB/release 流程才能创建。

## 5. Trace 与 Cluster 契约

### 5.1 `trace_cluster`

必填：`cluster_id`、`time_range_ns`、`root_event_ids`、`thread_refs`、`edge_summary`、`clock_quality`、`profile_id`、`selection_state`、`artifact_ref`。

`selection_state` 仅允许 `pending`、`selected`、`rejected`。`selected` 必须包含 `selection_actor`（`user`、`agent` 或 `system`）、`selection_rationale`、`action_id` 与 timestamp。`selection_actor=system` 仅允许由版本化自动化 policy 产生，额外必须记录 `policy_id`、`policy_version`、ranking/evidence 输入 hash 与排除理由。任一选择只表示工作流选择，不提升证据等级。

### 5.2 `hotspot_candidate`

必填：`hotspot_id`、`cluster_id`、`function_ref` 或 `unresolved_reason`、`active_time_metrics`、`temporal_pressure`、`critical_path_metrics`、`dominance_label`、`evidence_refs`。

`temporal_pressure` 至少为 `low`、`medium`、`high`；必须附 profile version、窗口定义与统计样本量。`dominance_label` 至少覆盖 `compute_dominant`、`blocking_dominant`、`io_dominant`、`driver_wait_dominant`、`unknown`。

### 5.3 原始 Trace 输入与转换证据

正式 trace 输入类型只有以下两个：

| `input_kind` | 原始 artifact | 规范化方式 | 结论边界 |
|---|---|---|---|
| `external_trace_analysis_report` | Trace 团队 JSON | `trace-result-ingestion` | 保留上游显式 hotspot / critical path / source-location 字段，但仍需 source binding。 |
| `raw_htrace` | `OHOSPROF` `.htrace` | `raw-htrace-analysis` 经 TraceStreamer 转换 | `partial` 的基础 span / scheduler 事实；不得生成自行推断的 critical path、源码位置、GPU 时间或 DMA 因果。 |

`.sys` 文本调度输入的 `sys_trace_experimental` 是 Spike，不属于正式主输入。它的结果必须携带 `experimental=true`。

`raw_htrace` 的 artifact 链必须为：

```text
raw_trace_input
  -> htrace_conversion
  -> trace_analysis_result(input_kind=raw_htrace)
```

若转换或解析失败，末端替换为 `htrace_analysis_failure`，原始 artifact 仍保留。`htrace_conversion` 至少记录输入 hash、converter binary hash/version/resolution、cache key、派生 DB hash/大小/受管路径、日志引用、schema hash 和能力探测。缓存 identity 必须包括输入 hash、converter binary hash 与 profile version；同一 identity 的转换必须互斥。

raw-htrace hotspot 的 `function_ref.resolution=raw_htrace_symbol_only` 不等价于 source location。`critical_path_metrics.available=false` 是强制语义，除非另一个版本化、可回溯的 trace 分析 artifact 明确补充关键路径证据。

### 5.4 `trace_event_catalog` 与 `trace_event_source_resolution`

`trace_event_catalog` 绑定一个 `trace_analysis_result` 和一个 `cluster_id`。每个 event 必填：`event_id`、`rank`、`event_name`、`event_kind`、`metrics`、`trace_event_refs` 与 `source_resolution`。raw `.htrace` 的 event 排名必须基于该 cluster 时间窗内的重叠贡献；artifact 的 `quality.profile_id` 必须标识排序规则版本，并记录候选上限、提取质量和原始 trace/转换 DB evidence。若 `event_name` 可被解析为 `H:<name>|<level><tag_bits>`，event 必须带可选 `hitrace_marker`，其中记录 level、拆分后的 tag bits/module tags、header SHA、mapping SHA、header origin；tag 目录不得作为无版本硬编码表使用。

`trace_event_source_resolution` 必须引用其 catalog/event，包含 `resolution.state`、`method`、`trace_binding_match_level`、`reason`、`source_candidates` 和可选 `code_block`。HiTrace 结果还必须保留 marker、tag catalog provenance、source-scope hint provenance/实际 roots、插桩宏/tag match 和可选文本化 `located_function`。当存在 `code_block` 时，`match_evidence.matched_lines` 必须明确区分插桩点、上游声明位置或文本匹配行；已验证 HiTrace 插桩还必须给出 `ast_target`（函数名、声明行、函数体起止行）。`ast_target` 的函数体必须实际覆盖插桩行，禁止仅按最近的前置函数定义归属；它只是**源码定位容器**，不是自动认定的热点代码范围。`resolution.state` 仅允许 `declared_source`、`symbol_text_candidate`、`symbol_text_reference`、`instrumentation_point_exact`、`instrumentation_point_function_derived`、`instrumentation_point_pattern`、`instrumentation_point_candidate`、`unresolved`。`declared_source`，以及“事件标签精确相等或由该函数的 `__func__` / `__PRETTY_FUNCTION__` 派生，且 serialized tag bits 已验证”的 `instrumentation_point_exact` / `instrumentation_point_function_derived`，在 `trace_binding_match_level=exact` 时可产生 development AST 输入；必须将宏、tag、函数和 snapshot evidence 一并保留。`symbol_text_*`、pattern、candidate 和 unresolved 仅能浏览/补充映射。任何 event-source evidence 都不能单独构成 trace-to-snapshot source binding。

对每个可进入 AST 的 event，后续阶段必须先产生 `trace_execution_slice`，再产生可参与 KB 匹配的 `code_feature`。该 artifact 至少包含：`anchor`（精确插桩行）、`measurement_scope`（`scoped_raii`、`explicit_pair`、`async`、`instant_or_counter` 之一及其语义证据）、`ast_container`、`local_slice`（AST/CFG 语句范围及控制上下文）、`calls`（切片内可解析 callsite、callee USR/定义位置与解析状态）、`dynamic_attribution` 和 `focus_candidates`。`measurement_scope` 必须引用版本化的 `trace_api_semantics` 记录，其中保存宏/API 展开链、定义 header 的 path/SHA、构造/析构或 start/finish/correlation 规则；不得用无版本硬编码规则猜测宏语义。例如 RAII 型 HiTrace 宏的计时范围从 marker 开始，到同一词法作用域结束；它不能被扩展为整个外层函数。没有可验证的配对、异步关联或词法范围时，`measurement_scope` 必须为 `evidence_required`，不得猜测结束行。

`focus_candidates` 只能是已证据化的 `local_slice` 或已归因的 `callee`，并带 `attribution_quality`：`static_only`、`span_attributed`、`sample_attributed`、`critical_path_attributed`。嵌套 HiTrace span 的 inclusive/self 差值只可形成 `span_attributed` 候选，不能证明 C++ 子调用 CPU self-time；callee 升级需要可关联的 span parent/correlation、CPU sample stack/IP-symbol map 或上游 critical-path edge。跨 Binder、FFRT、GPU 或无法解析的间接调用必须保留为边界/未归因耗时，不能把任意被调用函数标成热点。AST/KB 的输入应是 `focus_candidates`，函数级 feature 只允许作为无局部切片时的低置信度诊断回退，不能生成高置信度或 release 候选。

## 6. Knowledge Case 与正式 KB 的边界

`knowledge_case_catalog.json` 是可扩展案例库，不属于正式分层 KB。每个 case 必填：

```json
{
  "case_id": "case_image_blur_hpae_001",
  "schema_version": "1.0",
  "lifecycle": "candidate",
  "case_kind": "source_discovered",
  "algorithm_hypothesis": "image_blur",
  "target_hypothesis": "GPU_OR_AAE",
  "source_evidence": [{"path": "openharmony/...", "line": 1, "sha256": "..."}],
  "assumptions": ["hardware constraints pending"],
  "missing_evidence": ["capability", "benchmark", "correctness/fallback"],
  "promotion_links": []
}
```

`lifecycle` 仅允许 `candidate`、`evidence_collected`、`rejected`、`promoted`。只有 `promoted` 且引用已验证的正式 KB record 时，才可在机会报告中作为 `confirmed` 匹配来源。30 个 case 的目标是 discovery coverage，不是 30 个已确认硬件结论。

### 6.1 已有异构实现事实层

`existing_heterogeneous_implementation_cases.json` 是正式 KB 的源码事实层，不使用上述 `knowledge_case_catalog` 生命周期。它收录的是仓内已存在的 dispatcher、异构执行调用、设备/格式约束、零拷贝数据通路和 fallback 组件；一个端到端路径可按这些组件拆成多个可追溯记录。每条记录必须包含受控 `case_id`、`case_scope`、`implementation_kind`、`execution_roles`、`constraints`、`fallback`、`source_anchors`、`evidence_markers` 及 `provenance`。

事实层可以由逐条人工锚定 inventory 或版本化 AST 规则生成。后者必须输出 `discovered_implementation_case_inventory.json` 审计 artifact，记录规则 SHA、源文件相对路径、函数范围和调用行；规则只可在生产 C/C++ 函数体的代码区域匹配显式后端/设备 marker，必须排除注释、字符串、test、sample 和 fuzz。自动记录必须带 `implementation_family` 和 `discovery` 元数据，且 `discovery.evidence_reference` 必须在记录函数范围内。

事实层生成器必须实际验证每一个源码锚点和 marker，并把其解析出的行号和精确文件 SHA-256 写入记录。源码修改后无需先提交，但下一次提取必须更新受影响记录和 KB manifest；旧 hash 不得被静默复用。匹配命中事实层时，机会报告必须标明 `existing_implementation_reference` / “已有实现模式”，不得将它本身标为 `optimization_opportunity`。该层只证明源码中存在相应实现，不证明当前硬件、驱动、输入约束、性能收益或数值正确性；这些结论仍由其他 KB 层、benchmark 和场景证据独立承担。

### 6.2 异构机会评测契约

知识库质量与机会识别质量必须通过独立 `heterogeneous_evaluation_*` artifact 验证。`knowledge_case_catalog`、fallback 扫描候选和现有 KB record 只能作为**待标注来源**；它们不是 ground truth，也不得因工具自己发现过就被标成正例。

```text
source-backed discovery leads
  -> heterogeneous_evaluation_annotation_queue        (pending, not scoreable)
  -> independent annotation decisions                  (external, two reviewers)
  -> heterogeneous_evaluation_case JSONL               (approved oracle only)
  -> heterogeneous_evaluation_manifest                 (frozen snapshot / KB / policy / exclusions)
  -> heterogeneous_evaluation_run_input JSONL          (oracle-free recognizer input)
  -> heterogeneous_evaluation_prediction JSONL
  -> heterogeneous_evaluation_report
```

`heterogeneous_evaluation_case` 必填：`case_id`、`status=approved`、`split`、`benchmark_role`、`tasks`、`stratum`、一个 source snapshot 与带 path/line/SHA-256 的 locations、`oracle`、`leakage_guard` 和至少两名不同 reviewer 的 evidence。`oracle.classification` 仅允许：

| oracle class | 含义 | 正确工具行为 |
|---|---|---|
| `existing_heterogeneous_implementation` | 已有异构实现 | 识别实现/模式/映射并报告“已使能”，不能冒充新机会 |
| `optimization_opportunity` | 经约束、代价和证据复核后可新增异构优化 | 输出受约束的候选机会 |
| `ineligible` | 语义、硬件、搬运或成本条件否决 | 拒绝为异构机会并保留原因 |
| `evidence_required` | 当前证据不足以作结论 | 停止升级，列出缺失证据 |

`heterogeneous_evaluation_manifest` 必须锁定一个 source snapshot、KB manifest SHA、detector name/version、policy hash、已选 case hash、seed 和 leak exclusions。`generalization` case 的每个 oracle mapping ID 必须列入 exclusion，且必须使用 `masked_kb` 或 `masked_kb_and_source`：其 exact mapping/source anchors 不得存在于分析 KB/source view，预测也不得回显该 case 的 excluded mapping ID；`in_repo_coverage` 可以使用完整仓，但只度量已知实现覆盖率，不能被解释为泛化能力。识别器只可接收 oracle-free 的 `heterogeneous_evaluation_run_input`，不能接收 golden case 或完整 manifest。预测必须携带 manifest ID 和 case content SHA；缺失、重复、案例变更、额外预测、holdout ID 泄漏或 manifest 不匹配都必须拒绝评分。

评测报告至少计算：四类分类矩阵与 macro-F1、`optimization_opportunity` 的 precision/recall/F1、`ineligible` 误报率、`evidence_required` 非法升级率、`in_repo_coverage` 的 mapping Recall@K、`generalization` 的 target Recall@K、confidence calibration、分层覆盖度及 policy gate。未达到最小 case 数、最小类别覆盖或最小算法族覆盖时，终态必须是 `not_evaluable`，不能把小样本结果作为 KB 有效性的证明。

## 7. Page Action 与 Skill Manifest

`page_action` 必填：`action_id`、`version`、`risk_level`、`input_schema`、`output_schema`、`idempotency`、`audit_fields`、`implementation_ref`。

风险仅为：

| 级别 | 示例 | Phase A 策略 |
|---|---|---|
| `read` | 筛选、打开证据、KB 查询 | Agent full access |
| `write_local` | 上传 artifact、创建场景、启动 job | Agent full access，原子写和审计 |
| `resource_heavy` | Git clone、全仓扫描 | Agent 可请求，但必须通过硬门禁与显示执行计划 |
| `destructive` | 删除 connector/artifact | 本期不注册给 Agent |

每个 `skill_manifest` 必填：`skill_id`、版本、输入/输出 schema、执行器、资源上限、timeout、retry policy、determinism、cache key、错误码和 fixture。除这些执行字段外，还必须带 [skill_evolution_and_evaluation_spec.md](skill_evolution_and_evaluation_spec.md) 定义的 `skill_contract_version`、`skill_class`、`claim_boundary`、`required_evidence`、`quality_gates`、`evaluation`、`change_control` 和 `evolution`。运行时 registry 必须拒绝缺失治理 profile 的登记 Skill；模型不得自行修改 registry、prompt、阈值或 KB。

## 8. 单机 Git Connector 安全契约

1. 仅允许 `https://` 和 `ssh://` Git URL；拒绝 `file://`、本地路径、URL 中的控制字符和未知协议。
2. 默认 `git clone --no-recurse-submodules --depth 1`；禁止运行仓库脚本、hook、package install 或 build。
3. clone 前检查目标文件系统可用空间必须 `>= 80 GiB`，并保留 `10 GiB` 运行余量；检查结果写入 action audit。
4. 目标目录必须位于平台 managed clone root，不能覆盖已有非平台目录；失败/取消使用临时目录并原子 rename，保留日志和清理结果。
5. clone 后固定 commit SHA、生成 source snapshot、记录 remote URL 与默认分支；不因远端分支后续移动而改变 connector。

## 9. Artifact Store、恢复与配额

- 每个 artifact 写入临时文件，fsync 后原子 rename；manifest 最后写入。
- 使用项目级 lock 和 job idempotency key：`producer + version + input hashes + parameter hash + source binding`。
- Phase A 默认配额：trace/import 总量 10 GiB、缓存 20 GiB、managed clone 不计入 artifact 配额但受 80 GiB 门禁；原始 trace 的单文件上限通过 `HETERO_MAX_TRACE_UPLOAD_BYTES` 配置，默认 2 GiB。超限时拒绝新任务且提供按 hash 清理候选，不自动删除证据。
- `analysis_run` 有 append-only event log；服务重启时扫描 manifest 恢复 completed/failed，running job 标为 `interrupted`，不得假装完成。

## 10. API 兼容、错误与质量

- API 和 artifact schema 使用独立 semver；破坏性改变只允许新 major schema，不原地解释旧字段。
- 对有语义质量门禁的目录与分析 artifact，`operation_status` 只描述执行生命周期，`quality.status` 才描述内容质量。`quality` 至少包含版本化 `profile_id`、`status`、支持该状态的计数/断言以及未通过原因；消费者必须显式处理 `pass`、`warning` 和 `failed`，不得按 HTTP 状态或记录总数推断质量。
- 自建异构 API 知识库即使成功持久化，也可能是 `quality.status=warning`：页面必须展示公开可调用合同、算子/能力项、内部实现项等分类计数，Agent 必须先说明质量边界，再决定检索、缩小目录或重建。其具体门禁以 [自建异构单元 API 知识库构建 Spec](custom_accelerator_api_knowledge_base_spec.md) 为准。
- 自建库参与推理时，`custom:<kb_id>` 是唯一目录身份，`custom_api_kb:<kb_id>` 是路由证据，`custom_api_record:<record_id>` 是 API 证据。路由与机会工件必须绑定该库 `content_sha256`；删除或变更后不得回退到同名平台库或其他自建库。
- 错误使用稳定 code：`SOURCE_BINDING_UNKNOWN`、`TRACE_CLOCK_INVALID`、`TRACE_EVENT_UNSUPPORTED`、`HTRACE_HEADER_INVALID`、`HTRACE_READ_FAILED`、`TRACE_STREAMER_UNAVAILABLE`、`TRACE_STREAMER_TIMEOUT`、`TRACE_STREAMER_FAILED`、`TRACE_DB_INVALID`、`TRACE_DISK_SPACE_INSUFFICIENT`、`CLUSTER_NOT_SELECTED`、`KB_VERSION_INCOMPATIBLE`、`EVALUATION_CASE_UNAPPROVED`、`EVALUATION_MANIFEST_MISMATCH`、`EVALUATION_LEAKAGE_DETECTED`、`EVALUATION_NOT_EVALUABLE`、`DISK_SPACE_INSUFFICIENT`、`GIT_URL_REJECTED`、`ACTION_NOT_REGISTERED`。
- 每个 adapter/skill 必须有 golden fixture、expected artifact hash 或语义断言、性能上限和 failure fixture。
- `.htrace` adapter 必须有真实 OHOSPROF fixture、header-failure fixture、可选 SQLite schema 缺失的降级测试，以及 converter provenance 的语义断言。`.sys` clustering 在通过 Spike 门禁前持续标为 experimental：必须先证明现有 fixture 可提供所需调度/唤醒事件、输出可重复、且关键路径/压力标记有人工可解释样本。
