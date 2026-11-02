# BuildItem 条件并行模型 v1

本实现提供可复算的 Trace 解析、串行回放和条件并行核频搜索。当前离线包 `gallery-native-transfer-v1` 的状态为 **`legacy_compat`**：核型倍率和争用系数来自原生代理实验，尚无图库应用并行改造后的真机验证，也没有完整的计算 / 访存成本模型。界面与 API 将原始数据标为 `measured`，搜索结果标为 `conditional_prediction`。回放通过只说明输入账本和图可以复现原始时长，不能证明并行收益准确。

实现位于 `backend/parallel_modeling/`，仅使用 Python 标准库与项目已有 `PlatformError` / `ArtifactStore`，运行时不依赖外部 Kirin 目录、NumPy、Graphviz 或绘图库。算法来源和归档文件 SHA-256 记录于 `data/parallel-modeling/provenance.json`，初始化时校验数据包哈希。

新增「寻优校准」可生成离线微基准包、拟合实测参数并保存模型版本。下文固定数值仍描述内置归档模型；选用校准版本时，核型倍率、逐核组/逐负载类型的频率拐点、兼容回退弹性、争用与分支启动开销来自该版本，实测基线保持不变。分析支持 `model_package_id`；搜索省略此字段时继承分析使用的模型。校准流程与适用域见 [Trace 管理与寻优校准](trace_storage_and_calibration.md)。

## 数据与适用域

内置 `gallery-edit-pmu-20261014` 是 2026-10-14 图库编辑页同轮 Trace / PMU 解析归档：主线程 TID 40362，vsync 60209，UI callback 为 **86.940 ms**，应用 on-CPU 为 **76.094 ms**，26 个 BuildItem，65 次同步服务调用。VideoPlay 保留 21 次真实调用。组件 PMU 包含采样边界上下界，其中 9 个组件通过至少 3 个内部采样区间、指令边界不确定性不超过 25% 的特征规则。

历史 **90.409 ms / vsync 28031** 属于另一轮样本，不作为此包的基线，也不会与 86.940 ms 的 PMU 或时间账本拼接。内置包保存紧凑的原始解析输入和 PMU 特征，没有附带整段原始大 Trace；来源标识和原始算法哈希可追溯。新导入的原始 Trace / PMU 会保存在该数据集的受管输入目录，可重新解析。

硬件画像 `kirin-gallery-reference-v1` 只描述参考设备记录的资源和离线代理样本：

| 核组 ID | 物理核数 | 逻辑 CPU | 最大 OPP | BuildItem 回退倍率 | 其他 CPU 工作回退倍率 | 争用 α |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| big | 1 | 10、11 | 2,500,000 kHz | 1.000000 | 1.000000 | 0.112278 |
| middle | 3 | 4–9 | 2,151,000 kHz | 1.262300 | 1.355735 | 0.194944 |
| small | 4 | 0–3 | 1,600,000 kHz | 3.319963 | 4.625432 | 0.073157 |

核组共用一个频率域，主线程和 worker 落在同组时必须使用相同 OPP；逻辑 CPU 不等于独立物理核。OPP 候选严格来自归档列表。选择此画像表示条件分析的参考域，不构成对新设备身份、热状态或负载匹配的自动证明。未知画像拒绝搜索；未指定画像的导入仍可完成测量分析。

## 新采集如何进入模型

`ParallelModelingManager(state_root, artifact_store=None)` 暴露同步 `catalog()`、`import_dataset(payload)`、`analyze(payload, cancel_event=None, progress=None)`、`search(payload, cancel_event=None, progress=None)`、`get_dataset(id)`、`get_analysis(id)`、`get_search(id)` 和相应 `list_*()`。持久数据放在 `STATE_ROOT/parallel-modeling/`，HTTP / Agent 入口由 `ParallelWorkbench` 统一包装为后台任务。

导入只能选择一种源：

```json
{"builtin_id":"gallery-edit-pmu-20261014"}
```

```json
{
  "capture_run_id":"capture_...",
  "step_id":"step_07",
  "target_tid":40362,
  "target_vsync_id":60209,
  "hardware_profile_id":"kirin-gallery-reference-v1"
}
```

HTTP / Agent 服务将 `capture_run_id` 解析成受管 `scenario_capture_manifest/v1`，再传入管理器的 `capture_manifest` 字段。只导入一个已采集步骤；多个步骤时需指定 `step_id`。步骤 Trace 必须通过质量校验，文件路径必须在 `STATE_ROOT/scenario-capture` 内且 SHA-256 一致。部分采集允许保留合格 Trace，但 PMU 缺失不会被补成模型数据。

```json
{
  "trace_text":"完整 ftrace 文本",
  "pmu_text":"可选的 PMU CSV 文本",
  "pmu_metadata":{"target_tid":40362,"event_names":["cycles","instructions","generic_cache_references","generic_cache_misses"],"sync_begin_ns":0,"sync_end_ns":0},
  "target_tid":40362,
  "target_vsync_id":60209,
  "hardware_profile_id":"kirin-gallery-reference-v1"
}
```

上例 PMU metadata 是字段示意；必须提供该轮真实 metadata，零时间示例不能通过时钟验证。PMU CSV 与 metadata 缺一、目标 TID 不符或没有可验证的 `PMU_CLOCK_SYNC` marker 时，只保留不可用原因。HiLog 由采集流程归档，当前数值模型不把 HiLog 推测信息变成依赖边。

```json
{"parsed":{"frame":{},"binder_chains":{"chains":[]},"wait_ledger":[]},"hardware_profile_id":"kirin-gallery-reference-v1"}
```

`parsed` 的对象内容需使用归档解析 schema：`frame` 含 `start_ns/end_ns/duration_us/tid/vsync_id/spans/units`；每个 unit 含 `id/name/source_line/phases`，phase 的 `kind` 为 `on_cpu/runnable/blocked/unknown`，且所有 phase 严格连续覆盖 callback。BuildItem span 含起止 ns、行号以及 `CustomNode:BuildItem [组件名]`。Binder 记录需要精确请求、接收、回复、服务线程状态与调用方 blocked 账本，不能只用统计次数替代。组件 `component_features` 与 `pmu_quality` 可选。导入的解析证据标明“用户提供，未独立验证原始 Trace 哈希”。

原始 Trace 不指定 TID 时，只从实际 `UIVsyncTask` marker 的应用线程选择：优先使用该采集步骤的真实 target PID，否则要求唯一候选；设备 shell 的步骤 marker TID 从不冒充应用 TID。不指定 vsync 时，确定性选择包含完整 BuildItem 的最长完整 callback，并记录选择规则与候选 vsync IDs。`GALLERY_STEP/ACTION/HOLD` 只表示操作 / 采集边界，不会当作 callback、关键路径或显示完成。仅有这些外层 marker 的输入明确失败。

## 原始 DAG、回放与依赖

解析器把应用执行划分为非重叠 CPU、保留延迟和同步结果等待；精确事务 ID 连接 Binder send / receive，按服务线程同步调用栈及调用方 TID 配对 reply，保留服务 CPU、内部等待和返回至唤醒阶段。未知等待保留为原位延迟，不把 wakeup 单独认定为数据依赖。嵌套 BuildItem、多个请求共用模糊 blocked 区间、缺失对应等待的同步请求等当前无法安全表达的输入会被拒绝。

`observed_serial` 保留原执行顺序。串行回放使用实测成本、单位核型倍率、频率弹性 0、争用关闭、分支开销 0。回放门禁检查 callback、应用 CPU、服务 CPU 的误差均不超过 **0.01 ms**，且所有节点完成；失败后禁止寻优。服务同步调用数量、组件数量与 request IDs 在分析输出中完整保留。

时间账目同时保留墙钟、On-CPU 和 Off-CPU 三种语义。对原始串行阶段，`T_wall,observed = T_on_cpu,observed + T_off_cpu,observed`，其中 Off-CPU 由 `runnable/blocked/unknown` 构成；这是 Trace 观测账目恒等式。预测图保留非 Binder delay 节点的实测时长；`blocked` 中已配对的同步 Binder 等待由 send → service → reply → wake 依赖链和服务资源表达，不再重复放入固定 delay 节点。核频、核型与争用模型只换算 On-CPU 节点。

并行图默认 `preserve_observed`。每个所选 BuildItem 的内部顺序、请求返回依赖、主线程前段 / 后段 / 非候选工作和有限容量服务门均保留；只将所选组件组织为条件分支。其假设是所有分支输入在公共 fork 就绪、组件及其中 IPC 可在工作线程执行、主线程残余工作不生产分支输入、结果消费可延迟到公共 join。没有源码时不能证明这些假设，因此组件 `eligibility="conditional_assumption"`，并非已经确认线程安全。

每份图含完整 `nodes/edges/groups/terminal/assumptions`，另有用于 UI 的 `mermaid` 概览。概览实线表示原始顺序或 fork / join，虚线只表示真实 IPC 调用归属；全部请求 / 结果依赖仍以完整 `edges` 为准。关键链同时记录 DAG 依赖、执行槽释放与服务容量释放产生的因果边。

## 成本公式与校准

应用 CPU 节点的 `duration_ms` 是真实采样时源 CPU/核组/频率条件下的耗时，不能天然视为大核最高 OPP 的参考工作量。对负载特征页给出 compute / memory / mixed 候选类型，源硬件身份和驻留 coverage 已核验，且所选模型同时包含每条源驻留与目标条件的绝对曲线时，先还原统一参考工作量，再预测目标 On-CPU 成本：

```text
F[k,class](f) = L[k,class](f) / L[reference,class](f_reference)

R_source→reference = Σ_i (T_residency,i / F[source_core_i,class](source_frequency_i))
                   / Σ_i T_residency,i

W_reference = T_stage,on_cpu × R_source→reference

T_on_cpu,predicted = W_reference
                   × F[target_core,class](target_frequency)
                   × C[target_core, workload_pair](N)

L[k,class](f) = L_max_opp[k,class] × G[k,class](f)

G(f) = (fmax / f)^e_hi                                    , f >= fknee
     = (fmax / fknee)^e_hi × (fknee / f)^e_lo             , f <  fknee

0 <= e_hi <= e_lo <= 1
progress_rate = 1 / (curve_transfer × C)
```

`L_max_opp` 是同一代理在该核组最高 OPP 的实测单位工作量绝对时延，`G` 是该核组曲线内相对最高 OPP 的分段响应。两者共同构成“核组 × 任务类型 × OPP”的任务类型频点性能曲线，因此命中曲线时不再乘独立核型倍率。混合源驻留先以 `Σ(T_i/F_i)/ΣT_i` 得到调和归一因子，再作用于阶段完整 On-CPU 账目；这避免选择主驻留核、对源因子做算术平均或把全部时间套在一个频点上。`complete` 驻留合同接受 95%–105% 的边界误差，偏离近似 100% 时记为 tolerance risk；分类置信度低于 high 或证据质量非 complete 时也至少记为 evidence risk。这里的横轴是 CPU 频率；经典 Roofline 的横轴是运算强度，并结合计算与带宽上限。

每个核组、每个 compute / memory / mixed 代理至少需要 **6 个不同实测 OPP**：至少 5 个作为训练 OPP，另 1 个中间 OPP 完全不参与拟合，只作为独立留出 OPP。方案必须覆盖硬件最低和最高 OPP，并把二者留在训练集；最高 OPP 是归一化锚点。重复运行先按 OPP 取单位工作量 CPU 时间中位数；候选 `fknee` 只来自训练 OPP，`e_hi/e_lo` 在对数域约束拟合；留出 OPP 使用相同代理、不同工作量和独立 `run_id` 检查域内插值。阶段 PMU 分类只是一项候选证据：它选择已校准曲线，但不能自行生成拐点或证明 DRAM 带宽饱和。

发布参数的 `frequency_domain_khz` 只列训练 OPP。模拟器仅当目标频率位于 `[min(domain), max(domain)]` 时使用该类曲线；频率不必等于某个训练点，闭区间内的未见 OPP 按域内插值处理。方案将硬件 min/max 纳入训练，使同一硬件目录的正常搜索处于校准域内。旧版本没有合法 `frequency_domain_khz`，或搜索频率落在闭区间之外时，`profile_factor()` 不执行曲线外推，直接使用 `G_legacy`，并分别记录 `missing_calibrated_frequency_domain` 或 `outside_calibrated_frequency_domain`。

源归一和目标迁移分别 fail-closed。源、当前硬件画像和模型冻结的 `hardware_profile_sha256` 必须一致；源硬件身份未核验、驻留 coverage 不满足 `source_anchor_eligible`、任一驻留缺少核组/频率、源曲线缺失/域外或绝对参考锚不一致时，整个阶段不做部分反推。即使源归一单独可用，目标 workload class 或完整 `transfer_reference` 与源不一致，或者目标进入显式 elasticity/旧倍率路径，也停用源归一并以 `W_reference = T_on_cpu,observed` 回退，不能把绝对源曲线与旧 target ratio 混用。校准发布版本将硬件画像 SHA 冻结进 model/version 并纳入 model SHA；缺少该冻结身份的旧模型 fail-closed。目标阶段为 `unknown`、目标曲线缺失或频点域外时使用旧目标倍率/弹性：

```text
G_legacy(f) = (1 - elasticity[k]) + elasticity[k] × max_khz[k] / f
T_on_cpu,predicted = T_on_cpu,observed × R[k,category] × G_legacy(f) × C
```

`category` 区分 BuildItem 与其他 CPU 工作，只用于兼容回退。`C` 优先匹配经过留出验证的双任务有向类型/核组/频点单元；未知类型、未测条件或活动任务超过 2 时回退 `1 + alpha[k] × max(0, N-1)`。调度器在活动任务完成时重新计算 `N` 和速率，因此并发争用随执行变化。每个并行分支使用持久 Worker 的暖态任务派发开销；该值由同一目标核、OPP 和 payload 的直接批量路径与逐项队列路径配对差分得到。线程创建、绑核、ready 与 join 属于线程池冷启动，本轮未测，后续单独标定，不作为每个 BuildItem 的固定成本。

分析组件保存 `source_placement_cost_transfer`，搜索的每个候选和最终 `best` 保存实际使用量；`analysis_provenance/search_provenance.source_placement_cost_transfer` 只汇总源归一前提，`source_target_curve_compatibility` 另存隔离预测、候选和 best 的端到端 class/reference/target-curve 门禁及回退原因。源端回退只影响 On-CPU 参考锚；Runnable、Blocked、Unknown、同步服务链和服务 CPU 账目保持原 Trace/DAG 语义。

总时延不是 `Σ(T_on_cpu,predicted + T_off_cpu,observed)`。离散事件仿真以 DAG 依赖、应用执行槽、同步服务资源和完成事件推进：

```text
T_callback,predicted = makespan(DAG,
                               modeled On-CPU nodes,
                               observed non-Binder delay nodes,
                               Binder send/service/reply/wake edges,
                               application lanes and service capacities)
```

因此并行分支的执行与等待可以重叠；`app_core_ms` 表示应用 CPU 占用总量，`app_off_cpu_observed_ms` 表示原 Trace 的应用 Off-CPU 观测账目，`finish_ms` 才是配置的预测墙钟完成时间。`app_off_cpu_observed_ms` 不等于一段可直接加到预测 On-CPU 上的串行延迟，三者不可互换。

旧核型倍率来自同类原生代理串行 CPU 时间中位数，相对大核参考求比值；其他 CPU 工作保留单独的回退倍率。旧 `alpha` 来自同类代理的单跑/共跑 CPU 膨胀。这些参数仅为旧模型或主曲线/矩阵未覆盖条件提供兼容回退，不与命中的频点性能曲线或负载组合干扰矩阵叠乘。

内置旧模型默认 `elasticity=0.75`；0.5 / 0.75 / 1.0 是旧模型敏感性情景，不是统计置信度。选用实测校准版本时，名义仿真保持已拟合的逐核组/逐类型分段频响；不会在敏感性分析中静默替换为另一组旧弹性。服务阶段沿用记录条件下的延迟 / CPU 成本，通过每种服务默认容量 1 的 residence gate 表达竞争；服务内部等待仍占该有效服务通道。服务 CPU core-ms 单独累计，不与应用核心池争抢，因此整个模型不是完整 SoC 调度器。保留 runnable / blocked / unknown / 非 Binder 等待，不凭空消除背景影响。

PMU 仍不直接变成核型速度或拐点。阶段顶层分类保留 service / scheduler 等整体等待主特征；Running 区间独立生成 `classification.cpu_candidate`，下游 `workload_class` 优先使用该候选。相邻 snapshots 形成保守上下界，检查时间对齐、单调性、读错误和 enabled/running；规则基于 IPC 与已核验的 LLC MPKI 等证据生成 `compute|memory|mixed|unknown` Running 候选类型，再用该类型选择独立微基准实测得到的曲线。完整 PMU 事件不足时，现有 IPC、Cache/LLC MPKI 或基础计数可形成带置信度和缺失证据的降级候选；只有完全没有有效定量证据时才保留 unknown。generic cache misses 不能推出缓存层级、DRAM bytes、工作集或经典 Roofline 运算强度；其降级分型不得表述为已证实的硬件瓶颈。core-ms 表示资源占用，不是能量或功耗。

## 搜索空间、目标与输出

```json
{
  "analysis_id":"an_<24 hex>",
  "model_package_id":"gallery-native-transfer-v1",
  "candidate_ids":["build_01","build_02"],
  "dependency_policy":"preserve_observed",
  "objective":"minimum_frequency_with_budget",
  "budget_ms":86.94,
  "robustness":false,
  "space":{
    "main_core_group_id":"big",
    "available_core_group_ids":["big","middle"],
    "worker_counts":[0,1,2,3],
    "main_participation":[false,true],
    "frequency_policy":"all_opps",
    "frequencies_khz":{"big":[1325000,1440000],"middle":[1076000,1325000]}
  }
}
```

`candidate_ids` 默认全部组件。`main_core_group_id` 默认 big；可用核组默认三组；worker 数默认 `[1,2,3,4]`，受物理核数限制，**只有显式含 0 才把串行配置加入搜索候选**。所有 worker 在单一核组内，不支持混合核组 worker；每个允许的核组会分别枚举。主线程同组时占用一个物理核。`main_participation` 可为布尔或布尔数组，true 时枚举两种派发策略。`frequency_policy` 为 `all_opps` 或 `max_opps`，`frequencies_khz` 可进一步指定实际 OPP 列表。`core_kinds` 仅为旧兼容别名，正式字段为 `available_core_group_ids`。

默认空间包含 3348 个配置，逐一执行离散事件仿真；每个配置间检查取消，定期回报 `stage/percent/progress/completed/total`。`minimum_latency` 按延迟、核槽数、应用 core-ms 依次排序。`minimum_frequency_with_budget` 先过滤预算，再按“活动频域最大归一化 OPP、活动频域归一化 OPP 之和、核槽数、延迟”排序。未使用频域的 `frequency_vector` 为 null，不计入分数。

推荐按**应用占用的物理核心组合**分组：主核计一次，每个 worker 保留一个独立物理核，主线程参与并行复用原主核。主线程和 worker 同属中核时，1 主核 + 2 worker 是 3 中核；不能因核型相同而去重为 1 核，也不能按 SMT 逻辑 CPU 数重复计数。频点、主线程是否参与及派发策略是组合内部的搜索维度，不形成新的组合。每种组合分别按本次目标寻找最佳点，输出 `core_combination_recommendations`，因此「1 大 + 2 中」与「1 大 + 3 中」各有一条推荐，低频目标分别寻找各组合自己的最低可行频点。该计数表示分配的应用物理资源，不是实测同时忙碌的核数；服务 CPU 仍在应用核心池之外。

组合推荐先列可行组合并按原目标排序；预算不可行组合排在后面，按各自名义最短可达时延排序，同时保留其最快配置、频点及 `feasible=false / selection_status=budget_infeasible`。`best` 仍是全空间按原目标选择的全局最佳，`candidates` 保留所有频点与派发配置，组合表不裁剪搜索空间。所有组合都无解时仍返回 `parallel_modeling_no_feasible_configuration` 错误，details 含最短可达时延及组合矩阵，不生成虚假的可行最优解，也不在错误中重复附带全部配置。

`robustness=true` 对内置旧模型扫描 elasticity 0.5 / 0.75 / 1.0、服务容量 1 / 2 / 4、争用关 / 开的 18 个组合。预算目标在**每个物理核组合内**按精确目标顺序验证，分别选首个 18 情景全部满足预算的配置；某组通过不代表另一组通过。名义不可行点已不满足预算；名义可行但未做敏感性检查的候选仍为 `feasible=null`。若某组所有名义可行点均未通过敏感性检查，该组保留名义最快配置作为不可行诊断，不标为稳健推荐。延迟目标只复核各组合的名义最快配置，不声称已求得最小最坏情景延迟。选用实测校准版本时保持已拟合的逐核组/逐类型分段频响及其明确回退，只扫描服务容量与争用，共 6 个情景。所有情景结果都属于模型条件分析，不等同于真机验证或置信区间。

归档参考核组和全部组件下可复算：

| 条件 | 结果 |
| --- | --- |
| 大核参与、3 中核、最大 OPP、main_first | 53.873030 ms；应用 109.835748 core-ms |
| 86.940 ms 预算的名义低频配置 | big 1,325,000 kHz / middle 1,076,000 kHz；86.465873 ms |
| 该预算的 18 情景低频配置 | big 1,440,000 kHz / middle 1,325,000 kHz；最坏情景约 86.868 ms |

HTTP 路径见 [场景采集与并行建模工作台](scenario_capture_parallel_modeling_spec.md)。核心结果字段如下：

| 对象 | 主要字段 |
| --- | --- |
| dataset | `id/name/status/source_kind/hardware_profile_id/summary/provenance/quality/artifacts` |
| analysis | `id/dataset_id/baseline/replay_gate/components/statistics/requests/states_ms/graphs/cost_model/analysis_provenance/assumptions`；每个有特征版本的组件附 `source_placement_cost_transfer` |
| search | `id/analysis_id/objective/budget_ms/search_configuration/best/core_combination_recommendations/candidates/pareto/candidate_ids/search_provenance/model_package/assumptions`；provenance 保存源驻留归一覆盖与回退 |
| best | `configuration/metrics/comparison/frequency_vector/frequency_unit/critical_chain/critical_edges/timeline/graph/node_start_ms/node_end_ms/component_costs/resource_costs/time_accounting/frequency_model_usage/source_placement_cost_transfer` |
| configuration | `name/main:{kind,khz}/workers:[{kind,khz}]/main_participates/dispatch` |
| metrics | `finish_ms/app_core_ms/app_off_cpu_observed_ms/service_core_ms/service_queue_sum_ms/service_queue_max_ms/peak_app_cpu_concurrency`，敏感性检查后增加 `scenario_worst_ms/scenario_count` |
| comparison | `baseline_ms/latency_gain_ms/latency_gain_pct/app_cpu_delta_ms/cpu_resource_gain_pct` |

`critical_chain` 为节点 ID 数组；timeline 每项含 `node/group/kind/resource/request_id/lane/start_ms/end_ms`。`time_accounting` 记录参考 On/Off-CPU 账目、状态拆分、预测 makespan 和 Off-CPU 保留策略；`frequency_model_usage` 记录目标分段曲线覆盖的 core-ms、旧弹性回退 core-ms、命中的核组/类型/拐点/训练频率域和回退原因；`source_placement_cost_transfer` 独立记录源驻留归一覆盖的预测 core-ms、组件和回退原因，避免把“目标曲线命中”误读为“源锚也已核验”。频率统一为 **kHz**，显示 MHz 时需除以 1000。`pareto` 是延迟 / 应用 core-ms 的非支配前沿，不等同于低频目标排序。搜索记录包含模型 SHA-256、归一化输入哈希、实际配置数、敏感性参数及覆盖范围。

`candidates` 明确将所选 `best` 放在首位，随后是按目标排序的名义可行配置，最后是名义不可行配置。每行有 `comparison/meets_budget/nominal_feasible/feasible/sensitivity_status/rank_scope`。稳健预算搜索中，名义满足预算但尚未做声明情景验证的候选 `feasible=null`，不会继承最佳推荐的验证状态；名义已经超预算的候选为 `feasible=false`。`rank_scope` 与 `search_provenance.candidate_order` 说明剩余行只按名义目标排序。

`core_combination_recommendations` 每项平铺对应候选的配置、指标、频点和可行性字段，不重复保存完整 timeline。附加合同如下：

| 字段 | 含义 |
| --- | --- |
| `combination_id / combination_label` | 稳定组合键及名称，例如 `big1-middle3-small0`、`1 大 + 3 中` |
| `physical_core_counts / physical_core_count` | 按 big/middle/small 分组的物理核数及总数 |
| `rank / candidate_count / nominal_feasible_count` | 推荐排序、该组合全部配置数、名义时延在预算内的配置数 |
| `selection_status` | `objective_optimum` 或 `budget_infeasible`；延迟目标不把预算作为约束 |
| `recommendation_basis` | `nominal_objective`、`declared_scenarios_objective` 或 `fastest_nominal_fallback` |
| `fastest_configuration_index / fastest_finish_ms` | 该组合的名义最快配置索引及其时延，不一定是低频目标选中的推荐 |
| `global_fastest_finish_ms` | 本次搜索全部配置中的最小名义仿真时延，非实测值 |
| `latency_gap_ms / latency_gap_pct` | 当前推荐比上述全局名义最快点增加的时延及百分比；百分比分母为全局名义最快时延 |

每条候选另有 `core_combination_id` 用于与组合推荐关联。非全局最佳推荐没有自己的完整时序图，不能把 `best.timeline` 或 `best.component_costs` 当作其他推荐的明细。`search_provenance` 记录分组、排序及差值基准，输入哈希的引擎版本加入 `core_combination/v1`，避免新结果覆盖同输入的旧合同记录。

### 寻优目标的精确定义

令配置决策 `x=(g_main,g_worker,n_worker,p_main,d,f)`，`Ω_current` 是当前页面选择形成的有限配置集合：候选任务受已观测依赖约束，Worker 来自同一核组，Worker 数受物理核容量约束，频率只能取所选硬件 OPP。`T̂_UI(x)`、`C_app(x)` 分别是 DAG 仿真的 `finish_ms` 与 `app_core_ms`，`N_slot(x)=1+n_worker`，`i(x)` 是稳定配置索引。

- **最短时延**使用字典序目标 `lex min_x (T̂_UI(x), N_slot(x), C_app(x), i(x))`，约束 `x∈Ω_current`。开启稳健性时，只对每个核心组合的名义 winner 做声明情景敏感性检查；它不是 `min max_s T̂_UI(x;s)`，也不把敏感性结果加入最低时延可行域。
- **预算内最低频点**令活动核组归一化频点 `r_g=f_g/f_g,max`，使用字典序目标 `lex min_x (max_g r_g, Σ_g r_g, N_slot(x), T̂_UI(x), i(x))`，约束 `T̂_UI(x)≤B` 与 `x∈Ω_current`。开启稳健性时再加入 `max_{s∈S_decl} T̂_UI(x;s)≤B`；`S_decl` 是保存到 provenance 的有限扰动情景，不是统计置信区间。

引擎先按物理核心组合分组，在每组内使用对应字典序选择 winner，再从各组可行 winner 中使用同一目标选择全局 `best`。预算不可行的组合仍保留该组合的名义最快配置作为诊断，但不能进入全局可行 winner 集合。

最低频点目标比较的是活动频域的归一化 OPP，不等同于能耗目标；当前 `core-ms` 也不能代替功耗或能量。页面在搜索目标附近直接展示上述公式，并随目标、预算和稳健性选项切换。

新搜索在 `search_configuration` 中冻结目标、预算、候选任务、成本/特征版本、解析后的搜索空间、稳健性开关和依赖策略。解析后的空间显式补齐主线程核组、参与核组、Worker 数、主线程参与方式及频点策略。加载历史搜索、异步任务结果或助手结果时，页面从该冻结合同恢复控件；旧记录只读取确实存在的 `search_provenance.space` 和 `robustness_meaning`，缺失字段保持当前控件，不从 winner 反推原始输入。

`best.component_costs` 按组件汇总预测应用 / 服务 core-ms、分支启动开销、起止时间及完成跨度，并标记 `conditional_prediction`；`resource_costs` 按应用执行槽和服务分别汇总 CPU 占用，分别与对应总 core-ms 守恒。它们与 `analysis.components` 的实测 PMU / CPU 特征分别保存。

## 验证

`python -m unittest backend.tests.test_parallel_modeling -v` 覆盖归档回放、真实 PMU 边界和调用数、预测数值复现、每条 DAG 边的开始 / 完成时序、预算与敏感性情景、原始 Trace 自动选择与精确 Binder 配对、物理核数 / 共频域约束、候选组件生效、串并行枚举、受管路径 / 哈希 / 质量拒绝、未知模型 / 硬件、错误输入、取消及持久化。`python -m unittest backend.tests.test_source_placement_transfer -v` 覆盖中核非最高 OPP 源到目标核的迁移、跨核组/频率混合驻留的调和归一、未核验源画像与低覆盖 evidence 的 fail-closed 回退，并验证选中的 feature profile 进入分析组件、搜索模型与 provenance。`python -m unittest backend.tests.test_frequency_roofline -v` 独立覆盖分段曲线连续性、5 个训练 OPP 的拐点/双指数恢复、独立留出 OPP、compute 与 memory 的响应差异、训练域外回退、Off-CPU 不随频率缩放，以及逐核/逐类拟合发布。HTTP/Desktop 集成测试另外验证实测预约、SFTP 路径/目标/哈希围栏、拟合和成功/失败清理。上述验证不包含手机上的图库并行改造实验，也不表示代理曲线已经等价于目标应用阶段。

## 阶段特征与双任务干扰扩展（2026-10-21）

分析和搜索接受可选 `feature_profile_id`。分析绑定的版本默认被搜索继承；搜索显式传空字符串表示使用无分型的通用模型。后端验证 profile 内容哈希、parsed/extracted 数据指纹和硬件绑定，拒绝跨数据集或过期版本。分析展示阶段特征；搜索把 `component_classes` 传入每一轮名义、敏感性和最终时序仿真。search ID 纳入特征版本和引擎扩展版本，保存完整使用版本。

单任务参考工作、核型及频率响应保留；共跑系数优先按双方类别、核组和频点匹配验证后的 N=2 定向矩阵。未知或未覆盖区间回退原 alpha；该覆盖与回退在每个候选 `interference` 字段中输出。单独执行、实际串行 replay 和服务 CPU 账目保持原语义。特征合同见 [负载特征模型](workload_features_model.md)，校准及模型域见 [双任务干扰校准](parallel_pairwise_interference_v1.md)。


## 已核验归档的硬件匹配

图库全事件组 PMU 示例通过 `gallery-edit-all-pmu-hardware-20261023.json` 记录硬件关联证据：同一采集设备、历史 1 大 / 3 中 / 4 小物理核拓扑、本轮全部 CPU 的频点集合一致，且所选回调运行于 CPU10 / 2.5 GHz，与参考模型锚点一致。文件随仓库部署并纳入 SHA-256 校验。

对于历史上未绑定硬件的导入记录，分析与搜索按完整 `parsed` 内容的 SHA-256 及原始 Trace SHA-256 精确匹配该证据。匹配成功后，搜索使用参考硬件，并在结果 `hardware_resolution` 中记录匹配来源、证据摘要和频率锚点；不修改已存数据集、原始硬件字段、数据集 ID、特征版本或 PMU 质量。已有分析可直接继续搜索。不同 Trace、不同阶段内容或未知硬件不会因名称相同而匹配。

硬件对应关系已核验，与模型的应用收益验证状态分别保存。核型倍率和共跑争用仍来自原生代理校准，频率响应仍按所选模型的条件参数推演；PMU 复用估值不会提升为精确计数或芯片实测收益。
