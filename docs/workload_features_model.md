# 负载特征模型

状态：Normative v1.6；具体特征引擎版本随 profile 保存。

## 目的与边界

负载特征模型从实际 Trace 中描述每个 BuildItem 的计算、访存和调度特征。并行建模使用同一阶段 ID 和依赖图，结合硬件资源、核频迁移参数与共跑校准参数预测执行时间。分型是有证据的候选分类，不等同已确认的瓶颈或并行收益。

当前支持完整、不嵌套的 BuildItem 范围；复用导入器的边界、线程、状态守恒与 Binder 匹配校验。阶段以 Trace 起点排序，不按组件名称或经验特征改写类型。UI 回调中 BuildItem 之外的工作单独计入余量，不消失、不混入各阶段。

## BuildItem 的调度耗时拆解

每个阶段的墙钟耗时由互斥区间组成：`Running + 抢占后等待 + 其他 Runnable + Blocked + Unknown`。原有 `runnable_ms` 继续表示两类就绪等待的总和，便于历史模型兼容；界面不会再把总 Runnable 与其抢占子项重复相加。

| 项目 | 判定与含义 | 分析边界 |
| --- | --- | --- |
| Running | 目标线程被 `sched_switch` 切入后，到切出前的 CPU 驻留时间 | 包含用户态、内核态及可能的中断驻留，不直接等同纯应用计算时间。PMU 默认只统计目标线程用户态。 |
| 抢占后等待（Interrupted） | 明确抢占切出后，到再次得到 CPU 前的就绪等待 | OpenHarmony Linux 调度 Trace 的 `prev_state=R+` 提供抢占标记；普通 `R` 不足以确定是抢占。该项不是硬件 IRQ 耗时。 |
| 其他 Runnable | 被唤醒后尚未调度，或其他没有明确抢占证据的就绪区间 | 历史数据若未保留切出原因，显示未区分，而不是宣称没有抢占。 |
| Blocked | 切出时不处于就绪态，到被唤醒或恢复前的区间 | 可能涉及同步服务、锁、条件变量或 I/O；`S` 表示可中断睡眠，`D` 表示不可中断睡眠，不能直接解释为抢占或磁盘 I/O。 |
| Unknown | Trace 边界或事件不足，无法确认状态的区间 | 保留在总耗时中，保证时间账目闭合。 |

同步 Binder 等待通过请求、服务端接收、回复、调用方接收及目标线程阻塞区间关联，仅计入 Blocked 中重叠的部分。服务端 CPU 时间和 RPC 总往返时间是关联指标，不再额外加到调用方墙钟耗时。无法关联的阻塞保留为原因未确认。嵌套中断或软中断即使可观察，也应作为 CPU 驻留内的附加说明，不能与 Running 重复累计。

调度语义参考 [OpenHarmony Linux 的 sched_switch 实现](https://github.com/openharmony/kernel_linux_5.10/blob/master/include/trace/events/sched.h)；具体设备以其实际导出的 Trace 格式和证据为准。

### 实际逻辑 CPU 驻留与核组映射

每个 BuildItem 的运行位置来自真实 Trace。解析器按目标线程的 `sched_switch` 切入、切出事件形成 CPU 驻留切片，并将同一阶段、同一逻辑 CPU 和同一已观测频率的重叠时间累加到 `scheduling.cpu_residency`。每条驻留记录保存 `cpu`、`frequency_khz` 和 `cpu_ms`；频率事件缺失时仍保留真实逻辑 CPU，频率为 null。`cpu_migrations` 按阶段内相邻驻留切片的 CPU 变化计数，不能由 PMU 采样线程所在 CPU 替代。

当数据集绑定 `hardware_profile_id` 时，特征引擎读取该硬件画像的 `core_groups[].logical_cpu_ids`，把每个实际逻辑 CPU 映射为 `core_group_id`、`core_group_name` 和 `proxy_kind`。映射只接受硬件画像中的显式成员关系，**禁止根据 CPU 编号大小猜测大核、中核或小核**。未绑定硬件画像、逻辑 CPU 不在画像中或旧版本没有保存映射时，CPU 编号继续显示，核组字段保持 null，页面标记“核型未知”。`proxy_kind` 仅表示未校准模型继承哪类参考系数，不是对该 CPU 微架构的独立测量结论。

同一阶段可能在多个逻辑 CPU、多个频点甚至多个核组上运行。`scheduling.cpu_placement` 保存：

- `primary_cpu` 与 `primary_cpu_ms`：按全部频率切片汇总后驻留时间最长的实际逻辑 CPU；相同时选择编号较小者，保证结果可复现。
- `primary_core_group_id/name` 与 `primary_proxy_kind`：主驻留 CPU 在绑定硬件画像中的映射。
- `observed_cpu_ids`、`observed_core_group_ids`：阶段实际观察到的 CPU 与已知核组集合。
- `mixed_core_group_residency`：已知核组集合超过一个时为 true；未知映射不会被虚构成一个新核组。
- `source=trace_sched_switch_cpu_residency` 与 `cpu_migrations`：明确数据来源和迁移次数。

最长驻留 CPU 只是已观测切片中的排序结果。平台另外以 `stage.cpu_ms` 作为优先期望值（旧数据缺失时才回退 `states_ms.on_cpu`），汇总全部驻留切片得到 `observed_on_cpu_ms`，并保存 `cpu_residency_evidence/v1`：

| `status` | 覆盖判据 | 页面与建模语义 |
| --- | --- | --- |
| `complete` | `0.95 ≤ observed / expected ≤ 1.05` | 只表示时间覆盖完整；是否可作源成本锚还取决于硬件身份、逐段核组和频率。 |
| `partial` | `0.70 ≤ observed / expected < 0.95` | 显示“部分观测”和观测/期望时长；不把最长可见 CPU 表述为整阶段主驻留。 |
| `insufficient` | `0 < observed / expected < 0.70`，或观测时长比期望值高出 5% 以上 | 显示“证据不足”；只保留诊断信息，禁止作为确定源锚。 |
| `unavailable` | 没有可用期望 CPU 时间或没有任何驻留切片 | 显示驻留证据不可用；禁止源锚迁移。 |

该对象同时保存 `coverage_ratio`、`expected_source`、阈值、`reason_codes`、`temporal_coverage_complete`、`source_cost_anchor_preconditions_met` 和 `source_anchor_eligible`，并镜像在 `scheduling.cpu_residency_evidence` 与 `cpu_placement.residency_evidence`。最终 eligibility 要求时间覆盖完整、硬件身份已核验，并且每条驻留都有已映射核组和正频率；绝对曲线与参考系继续由消费端门禁。这样即使 Trace 首尾截断、硬件绑定只是用户选择或部分频率缺失，也不会被误认作确定源成本锚。旧 profile 没有 v1 contract 时只保留历史展示与严格守恒兼容诊断，不凭缺失的硬件内容身份提升为确定源锚。

执行时间迁移不以“主驻留核组”代替完整运行历史。只有 v1 驻留证据为 `complete`、覆盖率落在闭区间 `[0.95, 1.05]` 且明确允许作为源锚，并且每条驻留都能映射到同一模型参考系下的“核组 × 任务类型 × 频率”绝对曲线时，才使用全部驻留。先用已观测驻留求源条件到统一参考条件的调和归一因子，再将该因子作用于阶段完整 On-CPU 账目：

```text
R_source→reference = Σ_i (T_residency,i / F_source,i) / Σ_i T_residency,i
W_reference = T_stage,on_cpu × R_source→reference
T_target = W_reference × F_target
```

这等价于按驻留时间计算源因子的调和归一，能同时处理跨逻辑 CPU、跨核组与跨频点运行。`complete` 合同容许 Trace 边界带来的 ±5% 覆盖偏差，因此将已观测驻留组合视为整阶段的代表性组合；偏离近似 100% 时标记 tolerance risk。低置信度或非完整 PMU 的 Running 分类仍可选择曲线，但 provenance 至少为 evidence risk，不能表述为完全核验。源、当前硬件画像和成本模型必须冻结同一 `hardware_profile_sha256`；源、目标还必须使用同一 workload class 和严格相同的 `transfer_reference`。目标进入显式 elasticity 或其他旧倍率路径时，绝对源归一同步停用，整阶段回退旧参考锚，禁止混合两套工作量单位。回退原因随分析和搜索 provenance 保存。Running 之外的 Runnable、Blocked、Unknown 与服务账目不参与该换算。

## 运行期 PMU 特征与采集方式

新建采集计划默认使用 `auto`，`all` 为同义模式。在同一次场景操作中请求多个独立计数组；每个专题组携带自己的 cycles/instructions，以便 IPC、MPKI 等使用同组分母。不会为了收齐不同事件自动重放用户操作。既有单组计划继续保持原配置。

| 计数组 | 事件与特征 |
| --- | --- |
| 基础 | cycles、instructions、IPC |
| 通用缓存 | cache references、cache misses、Cache MPKI |
| 分支 | branch instructions、branch misses、分支失败相关特征 |
| 流水线 | frontend/backend stall cycles 与对应周期比率 |
| L1D | 数据缓存访问、未命中与 MPKI |
| LLC | 末级缓存读访问、读未命中与 LLC MPKI；具体缓存层级仍需核验 |
| DTLB | 数据地址转换缓存访问、未命中与 MPKI |

芯片上的硬件计数器数量有限，多组采集可能由内核分时复用。平台逐组保留启用时间、实际运行时间、不支持事件及读取错误，不把事件打开成功等同完整采集。缺少某一事件时保留其余可采事件，不把缺失记作零。多组采集增加读数开销，实际间隔可能高于请求值；内存预算调整后的有效间隔也单独记录。

精确阶段计数、无复用时的阶段保守上下界、内部完整窗口估计、采样窗口时间重叠估算分开保存。主展示优先精确计数，其次是带“估算”标记的单值；原始上下界、覆盖与边界原因保留在详情。复用缩放值不是严格上界，估值不外推到阶段未覆盖的部分。跨组计数不能拼接为同步比率。旧归档只有通用缓存事件时，不会生成虚构的 LLC 或分支数据。

### 按采样窗口重叠比例得到单值

计数器周期性读取累计值，BuildItem 的边界来自 Trace。对同一目标线程、由同步标记验证为同一时钟的相邻快照，先以每次读取起止时刻的中点作为快照时刻，再将相邻中点组成一个采样窗口。中点只用于确定读取时刻，不是把计数上下界取平均。窗口彼此不重叠，相邻阶段的分摊因此可以守恒。

对每个有效窗口分别计算：

`窗口计数增量 × (enabled_ns / running_ns) × (窗口与阶段的重叠时长 / 窗口时长)`

所有相交有效窗口相加，得到本阶段已覆盖部分的计数估算。无复用时缩放系数为 1；有复用时先逐窗缩放，再按重叠比例分配，不能先平均各窗的运行覆盖率。无实际计数器运行、读取错误、计数回退和时钟未验证的窗口不贡献数值，缺少样本不会当作 0。缺口和采样范围外的时间不做外推。

该版本使用**墙钟时间重叠**，假设计数在每个窗口内均匀分布；没有按调度 Running 时间精确归因。因此，长采样窗口跨越运行与等待、不同工作阶段时，单值可能偏离真实工作分布。100% 有效时间覆盖仅表示阶段位于可用窗口内，不代表精准采到阶段边界。计数器仍只统计目标线程用户态，单值不升级为精确计数、已验证分型或校准实测。

例如一个 176 μs 阶段被一个约 30.927 ms 的窗口包围，窗口累计 50,391,782 条指令，则主展示约 **286,774 条（估算）**。原始的 0–50,391,782 保守范围、内部完整窗口数 0、最大读取包络 30.928 ms 同时保留。下界为 0 不是没有执行指令，而是缺少能精确归属的内部计数。

IPC / MPKI 从**同一事件组、同一批有效窗口、相同权重**的计数计算，不能用 cache 组分子除以 base 组分母，也不能混用内部窗口计数与边界加权计数。分母为 0 时比率不可用。内部窗口 IPC 仍单列，并保持其原本仅内部覆盖的含义。

无复用时的保守上下界继续按原始读取包络计算：阶段内部完整窗口构成下界，与阶段相交的全部窗口构成上界。缩放后的估值不充当这个上界。减少实际采样跨度可改善估算，但请求 10 μs 不等于实际获得 10 μs 精度。

## 数据与证据契约

`workload_feature_profile/v2` 包含 `dataset_id`、`dataset_fingerprint`、`hardware_profile_id`、`hardware_profile_sha256`、`stages`、`component_classes`、`quality`、`provenance` 和 `profile_sha256`。指纹覆盖完整 parsed/extracted 输入、硬件 ID 与画像内容 SHA；版本 ID 还包含 producer contract、引擎版本和画像 SHA，因此固定内置硬件 ID 的内容升级也会生成新版本。模型使用前通过 `resolve_for_dataset` 核验版本内容、数据集、硬件和输入指纹。没有画像 SHA 的旧 v1 profile 仍可按旧指纹读取与展示，但 source transfer fail-closed，不会被提升为确定成本锚。

阶段 ID 沿用 `build_XX`。每个阶段包含：

- 计算：指令、周期、IPC、分支计数；精确值、估算单值与采样上下界分开记录。
- 访存：通用 Cache miss、LLC MPKI、Load/Store、访存停顿、DRAM 字节；缺失项为 null。
- 调度与服务：on-CPU、runnable、blocked、同步 Binder 阻塞、服务 CPU/停留时间、服务名称、真实逻辑 CPU/频点驻留、由硬件画像映射的核组、主驻留 CPU/核组和观察到的迁移；I/O 字节需单独的逐阶段实测证据。
- 分类：`classification.id = compute|memory|mixed|service|scheduler|unknown`；同时保存 `confidence.level/score`、`evidence_quality`、`available_metrics` 和 `missing_evidence`。当阶段以 Binder 阻塞或 Runnable 等待为主特征时，`classification.id` 保留 `service|scheduler`，`classification.cpu_candidate` 独立描述 Running 时段的 `compute|memory|mixed|unknown`。模拟使用 `workload_class` / `component_classes` 中的 Running 候选，不会因整体等待主导而丢失已有 CPU 证据。

PMU 来源：

1. 导入器已生成的同轮、目标线程 PMU 采样证据。`grouped_stage_pmu/v3` 保存 `time_weighted_estimates`、`weighted_time_coverage`、`weighted_runtime_coverage`、`weighted_bins`，方法为 `wall_time_overlap`，窗口约定为 `adjacent_read_bracket_midpoints`；原始 bounds 和 interior estimates 保留。`metric_evidence.estimate` 优先使用加权值，`estimate_scope=covered_stage_window`，`estimate_method=wall_time_overlap`；内部计数另存 `interior_estimate`，精确字段不被估值覆盖。
2. 可选 `workload_stage_measurements/v1` 实测上传。要求相同 dataset/hardware、目标线程、精确起止纳秒、目标线程计数作用域、Trace 单调时钟对齐说明、来源文件/采集 ID/工具版本。每个计数器有非负有限值、`count` 或 `bytes` 单位、事件语义。上传是用户实测声明，平台验证结构与关联，不宣称独立完成硬件认证。

上传按完整测量集合覆盖所选阶段的 PMU 特征，避免把不同采集轮次的计数器拼成 IPC/MPKI。未覆盖阶段继续使用原始 Trace PMU。上传只含 I/O 时，不提升 PMU 可用性。未对齐目标线程和时钟的整帧、整进程和系统级汇总不能按阶段耗时分摊；不以 blocked 推断 I/O 等待，不把通用 cache miss 乘缓存行大小变成 DRAM 字节。

## 分型规则与模型消费

规则版本 `evidence-tiered-stage-rules/v2` 保存于 catalog 和每个 profile provenance。分型先走完整证据路径，不满足完整 PMU 门槛时再走降级候选路径：

阶段主特征与 Running 负载候选是两个正交维度。Binder / Runnable 阈值先决定整体阶段是否为服务或调度主导；同一阶段的 Running 区间仍依据下列 PMU 规则生成 `cpu_candidate`。页面、JSON 与 CSV 同时展示两者。

| 条件 | 候选类型 |
| --- | --- |
| 同步 Binder 阻塞 / wall ≥ 30% | 系统服务等待 |
| Runnable / wall ≥ 30% | 调度等待 |
| CPU / wall ≥ 70%，可用 PMU，已核验 LLC 事件，IPC 下界 ≥ 1 且 LLC MPKI 上界 ≤ 5 | 计算主导候选 |
| 同样的证据与 CPU 覆盖前提，IPC 上界 ≤ 1 且 LLC MPKI 下界 ≥ 20 | 访存压力候选 |
| 上述计数证据充分但落在中间区间 | 混合特征，高置信度 |
| 有 IPC 但缺少已核验 LLC | IPC ≥ 1.2 为计算候选，IPC ≤ 0.8 为访存压力弱候选，中间区间为混合候选 |
| 有通用 Cache MPKI 或未核验 LLC MPKI | ≤ 5 支持计算候选，≥ 20 支持访存候选，仅作弱证据 |
| IPC 与 Cache 方向冲突、落在中间区间或只有可用基础计数 | 混合候选，低或中置信度 |
| 没有任何可用 IPC、Cache/LLC MPKI 或基础计数 | 证据不足，`unknown` |

数值是版本化分型阈值，不是所有芯片通用的性能诊断阈值。LLC 事件映射需要上传者明确提供 `event_mapping.llc_misses` 的 `verified`、`cache_level=LLC` 与事件定义，且 `event` 必须与 `counters.llc_misses.event` 完全一致。归档样本的通用 cache 事件可参与降级候选选择，但页面必须同时显示置信度、缺失证据和“不等同已核验 LLC / DRAM 瓶颈”。低 IPC 也可能由前端、分支或依赖停顿引起。高 LLC MPKI 仅表明访存压力候选，带宽争用及共跑影响仍需独立校准。

零值的证据语义按指标类型区分：当同组分母已有效采集时，`Cache MPKI=0` 等比率是可用的低值证据；但单独的 `instructions=0`、`cycles=0` 或 `cache_misses=0` 可能来自不支持事件或无计数器运行，不会被当成“已观测到 CPU / Cache 活动”。只有裸计数可用时，至少一个计数必须严格大于零才能生成最小证据的 mixed 候选；全零保留 `unknown`。

### 校准微基准与分型的对应关系

页面的“校准微基准设计”默认折叠，展开后说明三类 Running 候选对应的冻结代理、测量放置、观测指标和适用边界。频点性能曲线使用 `pair_work` v6：

| 代理类型 | 冻结源码结构 | 可审计的源码强度合同 |
| --- | --- | --- |
| compute | 每 work unit 调用 64 次寄存器整数混合依赖链，不访问代理工作集 | 640 个源码整数 payload 操作；没有显式流式字节分母，强度保持 null，不能写成无穷大 |
| memory | 顺序处理 1024 个 `uint64_t`，循环覆盖方案冻结的每线程工作集 | 最少读取 8 KiB、写入 8 KiB，2052 个源码整数操作，约 0.125244 操作/最小源码 Byte |
| mixed | 相同顺序读改写再叠加 64 次整数混合 | 相同 16 KiB 最小源码读写量，2692 个源码整数操作，约 0.164307 操作/最小源码 Byte |

源码整数操作数不包含循环、索引、地址生成和控制开销，也不等于 FLOP 或 retired instructions；最小源码 Byte 不包含 cache line、write allocate、预取或实际 DRAM 流量。三种标签是实验分组，不宣称代理与某个真实 BuildItem 指令流完全相同，也不保证单线程 memory 代理已经打满 DRAM。经典 Roofline 的计算强度阶梯是独立辅助实验，不替代频点—时延曲线。

每个硬件核组选择画像中一个代表物理核，将目标线程绑定到对应逻辑 CPU，按可控 OPP 扫描并核验实际落核、实际频率及同胞线程静默度。同一冻结任务在训练 OPP 重复测量，另留至少一个未参与拟合的 OPP 做域内验证。正式计时窗保存单位工作量 Wall / CPU 时延；受管微基准通过每线程、用户态 `perf_event_open` 同窗采集 cycles、retired instructions、通用 cache references/misses，并按 `time_enabled/time_running` 缩放，形成 IPC 和 Cache MPKI。每个事件保留 availability、原始/缩放值、enabled/running 时间及错误；硬件拒绝或不支持时为 null / unavailable，绝不由 workload 标签补值。硬件支持且事件语义核验后可增加 LLC MPKI，但通用 Cache MPKI 不能冒充 LLC 或 DRAM 带宽。

分型阈值把真实阶段放入 compute / memory / mixed 候选组；对应微基准实测曲线再把该阶段的 Running 时间迁移到目标核组和 OPP。分型本身不生成频率拐点、跨核倍率、共跑膨胀或加速比。

并行模型应保存选中的 `feature_profile_id` 及摘要。建模使用 `classification.cpu_candidate`（如有）或顶层 CPU 候选生成的 `workload_class`；顶层 service / scheduler 主特征继续解释 Off-CPU 账目。Running 阶段类型有三项受限用途：选择源核组/类型/频率曲线以还原统一参考工作量，选择目标核组/类型/频率曲线以预测迁移成本，以及查找对应核组、双方频点和工作集条件下的共跑参数。两类性能参数都来自独立代理微基准，PMU 候选分型本身不生成拐点、弹性、干扰系数或加速比。降级候选允许工作流继续，但搜索结果应保留其置信度与缺失证据。逐类频率曲线还必须命中其 5 个或更多训练 OPP 构成的闭区间；独立留出 OPP 只验证域内插值，不扩大训练域。源端画像身份、驻留 coverage、核组、频率和绝对曲线任一未核验时，不反推参考工作量；目标端无匹配校准、缺少合法训练域或频率在域外时，才明确回退旧核型倍率/频率弹性并标注来源。

## 页面与接口

页面“负载特征”包含数据集 / 特征版本选择、可选逐阶段实测补充、时间位置概览、可筛选逐阶段表格、三类指标详情、原始证据、导出与进入并行建模操作。逐阶段表格直接展示“运行 CPU / 核组”：主驻留逻辑 CPU、画像核组、主驻留时间、已观测频率和迁核 / 跨核组提示；阶段详情保留全部驻留。表格下方有两个默认折叠区：“特征分型说明”展示完整证据、降级候选和证据边界，“校准微基准设计”展示冻结任务结构、绑核控频、训练 / 留出及同窗 PMU 指标。用户主动展开时显示，页面初始状态不铺开长方法文字。所有后台失败显示可见错误；提取成功和特征完整性分别呈现。

阶段总览提供三种导出，均针对当前保存版本的全部阶段，不受页面筛选影响：

| 导出 | 行粒度 | 内容 |
| --- | --- | --- |
| 阶段 CSV | 每个 BuildItem 一行 | 相对回调起止时间、墙钟耗时、Running / Runnable / Blocked / Unknown、抢占与 Binder 子项、唤醒 / 迁核 / 服务调用、IRQ 注释、CPU 驻留、PMU 数值与逐指标状态；CPU 字段包括 `主要运行逻辑CPU`、`主要CPU驻留(ms)`、`主要核组ID/名称/代理类型`、`观测逻辑CPU`、`观测核组`、`跨核组驻留` 和 `CPU驻留_CPU@核组@kHz:ms` |
| PMU 明细 CSV | 每个阶段、每个指标一行 | 指令、周期、IPC、Cache / LLC / L1D / DTLB、分支、停顿、Load / Store、DRAM 与 I/O；包括对齐值、估值、保守范围、已观测贡献、组别、作用域、覆盖率与原因代码；同时重复 `主要运行逻辑CPU`、`主要核组ID/名称/代理类型`，便于每条指标独立关联观测核组 |
| JSON | 完整特征版本 | 原始结构、来源与版本指纹，保留现有导出能力 |

CSV 使用 UTF-8 BOM、中文列名和明确单位，可直接用 Excel 打开。数值列保留纯数字，未知值留空，实测零值保留为 `0`。`exact` 表示记录中的阶段对齐值，`estimated` 表示采样估值，`bounded` 表示只有范围，`unavailable` 表示缺失；不能把 `bounded` 的中点或已观测贡献补成阶段数值。IPC / MPKI 直接导出已保存的同组结果，不跨组重算。绝对纳秒时间仍保存在 JSON；CSV 使用相对毫秒时间，避免 Excel 对长纳秒整数的精度截断。Interrupted 是 Runnable 子集、Binder 是 Blocked 子集，服务端与 IRQ 耗时也分别标明归属。

下载接口为 `GET /v1/workload-features/profiles/{identity}/export.csv?view=stages` 和 `?view=pmu`。下载先校验保存版本的 SHA-256，读操作不重新采集、不重新分析、不改写特征版本。


助手请求发送前记录已保存版本集合；确认接收后，通过动作完成事件和最多五分钟的有界轮询载入当前数据集的新版本。旧版本、其他数据集、被拒绝 / 停止的请求不会自动回填；用户切换数据集、手动选择版本、上传测量或直接分析时取消该轮自动载入。手动选择的阶段在新版本仍存在时保持不变。单次读取设十五秒超时，离开页面时结束等待。

- `GET /v1/workload-features/catalog`：指标、分型规则、上传契约。
- `GET /v1/workload-features/profiles`：已保存摘要。
- `GET /v1/workload-features/profiles/{id}`：完整版本及证据。
- `POST /v1/workload-features/analyze`：`{dataset_id, measurements?}`；读取已有数据，生成不可变特征版本。

重复输入、测量和规则生成同一版本。特征引擎升级为 `1.6.0`，producer profile contract 升级为 v2，生成新版本并保留旧版本。v1 历史 profile 继续保持不可变并可显示原有 CPU / 核组；只有重新提取生成 v2 profile 后，才冻结硬件画像 SHA 与新的 source-anchor 前提。重新分析旧数据集时，如保留 `pmu.csv`、`pmu.json`、`trace.ftrace`，会重新验证 TID 和时钟同步标记，从原始窗口重建估值；重建文件与硬件画像的 SHA-256 纳入版本身份和来源。无原始采样的历史区间保持原值，不用中点补出数字。原始 parsed/extracted 和旧 profile 均不原地改写。记录位于平台 state root 的 `parallel-modeling/workload-profiles/`。

## 验证

`backend/tests/test_workload_features.py` 覆盖：归档真实样本的 26 个阶段与 PMU 上下界、无 PMU 的空值、Binder 时间守恒、同窗口分型、事件映射、真实 CPU → 核组映射、驻留缺口与混合驻留；v1.6 另覆盖未核验硬件、未知核组、部分未知频率不得成为源锚，以及固定硬件 ID 内容变化导致缓存失效、旧 v1 profile 仍可读取。`backend/tests/test_source_placement_transfer.py` 验证中核非最高 OPP、混合驻留调和归一、硬件 SHA、分类风险、覆盖容差风险、源/目标 reference 与 class 不一致和显式 elasticity 的 fail-closed 回退，并完成 feature profile → 分析 → 搜索集成回归。合成用例保留测试来源，不作为目标硬件实测验证。

2026-10-21 集成验证：完整后端测试运行 791 项，789 项通过、2 项跳过；前端 lint、TypeScript 与生产构建通过。浏览器以隔离 API 验证 26 阶段筛选、详情、导出、助手事件 / 轮询回填、版本选择保护及跨页面传递；1600 / 1280 宽度下人工看图检查。共跑校准方案 → 临时测量夹具 → 拟合 / 发布 → 并行搜索的页面链路保留所选特征与校准版本，并正确记录未知类别 / 超出双任务域的回退。夹具及其模型只写入临时状态，未写入用户平台数据；尚未执行目标真机校准或内部 Agent 平台联调。

2026-10-23 扩展验证：新增生产 C 采样器的模拟 perf 系统调用测试，覆盖多组、单事件失败、全部拒绝、计数器未运行、复用及缓冲限制；采集验证器核对运行增量和首尾读取内边界。负载特征回归覆盖分组局部分母、零运行组回退、短阶段边界、窗口过滤等价性、R+ 与普通 R、Binder 时间守恒及 IRQ 交叠。已有真实 Trace 在隔离目录重新导入得到 26 个阶段，调度分区与阶段耗时逐项守恒，并保留 176 μs 阶段的 0–50,391,782 指令区间及其 30.93 ms 读取跨度。前端提供精确值、保守区间和内部估计三种证据的浏览器用例；设备与 Agent 请求均隔离。新版 OHOS 采样器已交叉编译并纳入部署资产，本次未执行真机采集，设备事件支持与实际采样分辨率仍由后续采集记录确认。

1.2.0 回归覆盖窗口边界分摊、相邻阶段守恒、部分覆盖不外推、无效区间保留缺口、逐窗复用缩放、同组比率、旧原始数据重建与缺失原始数据回退。浏览器覆盖估算主数值、详情原始区间、精确测量和旧纯区间版本。
