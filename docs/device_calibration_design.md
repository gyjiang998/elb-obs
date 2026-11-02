# 真机自动校准设计

## 目标

寻优校准由平台在所选真机上完成。用户只选择设备和校准范围；平台负责发现硬件能力、生成冻结实验计划、部署受管微基准、执行频点与并发扫描、采集质量证据、拟合参数、做留出验证并发布硬件校准模型。结构与来源完整性检查仍是发布硬阻断；统计质量或覆盖门未通过、但已有可用实测和至少一个估计参数时，平台发布 `risk_calibrated` 风险校准版本，并随模型保留失败门禁和证据等级。JSON 导入只用于跨机器恢复和故障诊断，不作为默认工作流。

校准只改变条件预测使用的硬件参数。真实 Trace 中已经观测到的 Runnable、Interrupted、Blocked 和同步服务依赖保留为证据账目，不用 CPU 微基准重新缩放。

## 两类互补模型

### 硬件 Roofline

经典 Roofline 描述某个核组和频点的计算、访存上限：

```text
P_peak(g,f) = FLOP_source / t_compute
B_source(g,f) = Byte_source_min / t_bandwidth
I_source = FLOP_source / Byte_source_min
P_attainable(g,f,I) = min(P_peak(g,f), B_source(g,f) * I)
I_knee(g,f) = P_peak(g,f) / B_source(g,f)
```

- `I` 是计算强度。当前可发布的经典 Roofline 使用冻结源码的算法 FLOP 与最小源级字节，因此横轴是源级 FLOP/Byte；具备同窗 retired instructions 与经核验 DRAM bytes 后，才可另建 Instruction/Byte Roofline。
- `P_peak(f)` 是所选核组和 OPP 下以冻结源码算法 FLOP 计量的实测计算吞吐屋顶；Instruction Roofline 的指令吞吐屋顶属于另一套证据口径。
- `B_source(f)` 是受管流式内核的最小源级字节吞吐，不等同于内存控制器观测的 DRAM 带宽。
- 改变 CPU 频率主要移动计算屋顶；若内存或互联域也联动，平台同时实测带宽斜线，不能假定其恒定。
- 对固定负载，计算强度通常不因升频而改变。发生变化的是主导约束：低频时可能受计算上限约束，升频后可能受带宽上限约束。

### 任务类型频点性能曲线

并行仿真实际消费的是 On-CPU 服务时间。平台先按“核组 × 任务类型 × OPP”保存固定工作量的实测时延点，再用留出 OPP 验证连续分段插值：

曲线能力按核组判定，而不是按整台设备“一票否决”。计划用 `core_kinds` 冻结通用校准核组，用 `frequency_roofline_core_kinds` 冻结其中至少公开 6 个不同 OPP、可执行曲线训练与留出的子集。例如同一设备有 8 档和 5 档 OPP 两个核组时，8 档核组继续生成曲线，5 档核组不生成、不拟合也不伪造曲线；负载组合干扰、Worker 开销及其他已选参数组仍按各自能力覆盖 `core_kinds`。

```text
G(f) = (f_max / f)^e_hi                                           f >= f_knee
G(f) = (f_max / f_knee)^e_hi * (f_knee / f)^e_lo                  f <  f_knee
L(g,class,f) = L_max(g,class) * G(g,class,f)
0 <= e_hi <= e_lo <= 1
```

`f < f_knee` 时频率敏感度较高，理想计算受限任务接近 `1/f`；`f >= f_knee` 后，内存或共享资源逐渐成为上限，继续升频的时延收益趋缓。只有 `e_lo` 接近 1、`e_hi` 接近 0 时才近似理想的两段 Roofline；真实设备由测量拟合平滑过渡。

其中 `L_max` 是该核组、该任务类型在最大 OPP 下的单位工作量 On-CPU 时延。每个曲线点同时包含目标核组和目标频点的影响。目标阶段有受支持的负载类型和曲线时，模型以真实阶段的观测 On-CPU 时间为锚，按下式迁移，不再额外乘核型倍率或线性频率弹性：

```text
T_target,on-cpu = T_observed,on-cpu
                * L(g_target,class,f_target)
                / L(g_observed,class,f_observed)
```

两种模型不混用：经典 Roofline 解释物理上限并提供交叉验证，任务类型频点性能曲线用于时延预测和频点寻优。

## 需要校准的参数

| 层级 | 参数组 | 本轮校准参数 | 用途 | 当前真机实测方法 |
|---|---|---|---|---|
| 主模型 | 任务类型频点性能曲线 | `L(g,class,f)`、`f_knee`、`e_lo`、`e_hi` | 预测不同核组、任务类型和 OPP 下的 On-CPU 时延 | 计算、混合、访存代理在至少 5 个训练 OPP 实测，另留 1 个未见 OPP；保存原始点和单位工作量绝对锚 |
| 主模型 | 负载组合干扰矩阵 | 有向 `A←B` slowdown | 预测两个已知类别阶段共跑时的共享资源争用 | compute/memory/mixed 两两共跑，固定双方落核、频点和工作集，与匹配单跑锚做差分 |
| 主模型 | Worker 任务派发开销 | `O_dispatch,warm` | 估计持久 Worker 每次任务派发增加的关键路径时延和 CPU 成本 | 同一目标 Worker、同一 CPU/OPP、同一 payload 对比直接批量执行与逐项队列派发，分别记录 wall、producer CPU 和 worker CPU |
| 兼容回退 | 核型 CPU 倍率 | 每核组 `R_build`、`R_residual` | 旧模型或频点曲线缺失时迁移参考 CPU 工作量 | 相同代理和工作量在各核组最高 OPP 单线程运行，以线程 CPU 时间中位数归一 |
| 兼容回退 | 线性频率弹性 | 每核组 `elasticity` | 负载未分型或曲线适用域外时估计频率响应 | 至少两个 OPP 的旧线性模型 |
| 兼容回退 | 通用并发争用 | 每核组 `alpha` | 组合矩阵未覆盖或活动任务超过两个时估计 CPU 膨胀 | 同类代理按 1/2/3 活动任务实测，固定核组与 OPP |
| 辅助表征 | 经典计算与带宽 Roofline | `P_FLOP(g,f)`、`B_source(g,f)` | 解释平台计算和带宽上限，不直接替代任务时延曲线 | 固定浮点流水和大工作集流式内核分别测量源级 FLOP/s 与 Byte/s |

实际频率、落核、并发覆盖、重复波动和 thermal zone 原始快照是质量证据，不直接作为性能参数。当前以测量窗口频率是否稳定判断是否观测到有效热降频，不据此声称设备温度正常。Off-CPU 服务停留仍来自真实 Trace。

## 微基准矩阵

平台以受管、内容寻址的固定二进制执行以下实验：

1. **任务类型频点性能曲线**：计算、混合、访存代理按核组扫描可控 OPP，5 个训练频点拟合，至少 1 个独立频点验证；原始 OPP—时延点是主证据，拟合只用于域内插值。
2. **负载组合干扰矩阵**：三类代理建立匹配的单跑锚与九种有向两两共跑组合，保存双方核组、OPP、工作集和物理放置适用域。
3. **Worker 任务派发开销**：持久 Worker 在同一目标 CPU/OPP 执行相同 payload，比较直接批量路径与逐项投递、唤醒、执行、完成确认路径，使用配对差分拟合每任务暖态开销。
4. **兼容回退**：仅在需要生成旧模型参数时，运行对象树/整数控制代理的核组倍率、线性频率弹性和同类并发 `alpha` 实验。
5. **辅助 Roofline**：固定浮点流水、大工作集流式内核和计算强度阶梯用于解释硬件上限，不与主时延曲线叠乘。

每个条件先 warmup，条件按计划 ID 与条件 ID 的稳定哈希顺序执行，减少固定 OPP 顺序偏差；同一条件的重复样本连续执行。训练条件至少重复 3 次，留出条件至少重复 2 次。平台按 thermal zone 原始读数变化选择短冷却间隔，并以实际频率、落核和重复波动做质量门禁。

### 正式计时窗内的 PMU 旁路证据

受管微基准在目标工作线程内调用 `perf_event_open`，作用域固定为 `target_thread_user_only_timed_window`。四个事件加入同一 perf group。Worker 先通过 group leader 和 `PERF_IOC_FLAG_GROUP` reset/enable，再读取 wall/线程 CPU 起始时钟；工作负载完成后先保存线程 CPU/wall 结束时钟，随后才 disable、读取并关闭 perf 事件。因此 PMU 窗口略微包络正式计时窗，而 `wall_ms` 与 `cpu_ms` 明确排除 perf ioctl、计数读取和 fd 关闭开销。样本用 `pmu_window_relation=perf_group_encloses_timed_workload_window; timed wall/cpu exclude perf control/read overhead` 固化这一关系。当前固定采集四个通用事件：

- CPU cycles；
- retired instructions；
- generic cache references；
- generic cache misses。

perf 事件可能被内核复用。每个事件同时保存 `raw_value`、`time_enabled`、`time_running` 和错误码，并按下式缩放：

```text
count_scaled = count_raw * time_enabled / time_running
IPC = instructions_scaled / cycles_scaled
Cache MPKI = generic_cache_misses_scaled * 1000 / instructions_scaled
```

样本以 `pmu_collection_status=collected|partial|unavailable`、`pmu_window_aligned` 和逐事件 `pmu_counters` 保留可审计证据；扁平字段为 `pmu_cycles`、`pmu_retired_instructions`、`pmu_cache_references`、`pmu_cache_misses`、`pmu_ipc` 与 `pmu_cache_mpki`。缺少同窗分子或分母时，派生指标必须为 `null`，不能用任务标签推断或补造。聚合曲线分别保存每个指标的样本数，并另存四项齐全的交集样本数；不能用某一指标的最大覆盖数冒充完整 PMU 样本数。

PMU 是解释任务类型与曲线形态的旁路证据。权限不足、事件不支持或单个计数失败时，Worker 仍保留有效的时延、频率和落核测量；`partial` 或 `unavailable` 不单独阻断时延曲线拟合。此时平台只能降低对应微架构结论的证据强度，不能声称已经测得 IPC、Cache MPKI、LLC 流量或 DRAM 带宽。通用 cache reference/miss 的具体缓存层级由内核和 SoC 定义，也不能直接解释成 LLC 或 DRAM 事件。

### 尚待接入的证据与微基准

- **Instruction Roofline**：受管 Worker 已在同一正式计时窗口采集 retired instructions，但仍缺少经语义核验的 DMC/uncore DRAM bytes；当前整数混合轮次只是代理，不能当作硬件指令数。
- **缓存层级与 DRAM 屋顶**：需要跨 L1/L2/LLC/DRAM 工作集、线程数以及 Read/Write/Copy/Triad 内核；当前只有一个大工作集源级带宽屋顶。
- **延迟、TLB 与 MLP**：需要 pointer chase、stride 与多独立链微基准。
- **Worker 冷启动**：`pthread_create → 绑核 → ready` 与 `done → join` 是线程池生命周期的一次性成本，应单独测量和摊销，不能混入每个 BuildItem 的暖态派发常数。
- **IPC / futex / 迁移代价**：需要单独的受控回环和同步微基准；Worker 配对实验只覆盖同进程持久线程池路径。

### 计算强度的证据口径与最小实验矩阵

“计算强度”（Arithmetic Intensity，AI）是下列比值的统称，并不是独立于 FLOP/Byte、Instructions/Byte 的第四种指标。平台必须同时声明分子、分母以及二者是否来自同一计时窗口：

| 名称 | 定义 | 当前能力 | 可用于什么结论 |
|---|---|---|---|
| 源级浮点强度 | 固定内核的算法 FLOP / 最小源级字节 | 已支持。强度阶梯以每元素 24 B 的两次读、一次写为分母，2 / 8 / 32 / 128 / 512 个 FLOP 对应约 0.083 / 0.333 / 1.333 / 5.333 / 21.333 FLOP/B | 比较同一受管内核跨过计算屋顶和源级带宽斜线的位置 |
| 硬件浮点强度 | retired FP operations / DRAM bytes | 尚不具备通用计数。当前通用 PMU 没有已核验的 retired FP operation 与 DRAM byte 事件 | 只有获得同窗、可缩放且语义核验的 SoC 事件后，才能称为实测硬件 FLOP/Byte |
| 指令强度 | retired instructions / DRAM bytes | 校准 Worker 已通过 `perf_event_open` 提供目标线程正式计时窗内的 retired instructions；DRAM bytes 仍缺失 | 当前不能发布 Instruction Roofline |
| 源级整数代理强度 | 固定源码的 integer-mix rounds / 最小源级字节 | 已支持 `instruction_proxy`，但 round 不是 retired instruction | 只用于控制实验强度和回归，不替代 Instructions/Byte |
| LLC 填充线代理 | retired instructions / (`LLC read misses × 已核验 cache-line bytes`) | LLC read miss 事件在部分设备可用 | 只能标为 LLC-fill traffic 代理；它遗漏写回、预取、相干流量和其他主设备访问，不能重命名为 DRAM bytes |

负载特征采集侧的线程 PMU 目录包含 cycles、retired instructions、通用 cache reference/miss、branch、frontend/backend stall、L1D、LLC 与 DTLB 等事件；受管校准 Worker 当前只固定 cycles、retired instructions、通用 cache reference/miss 四项。两者都没有经核验的 L2/LLC 传输字节或内存控制器 DRAM 字节。通用 perf 事件在目标设备上还可能不可用或映射到厂商定义的不同层级，因此每个事件都要保留 event source、config、可用性、`time_enabled/time_running` 与缩放方法，不能仅凭事件名称推断物理含义。

任务类型频点曲线中的 `memory` 代理使用 64 MiB 单线程顺序块和标量 volatile 读写。它保证低源级运算/字节和跨工作集访问，但不保证一个小核或中核线程已经饱和 DRAM。标量发射、缓存填充、预取能力以及随 CPU OPP 联动的 cache/interconnect/DDR 频率都可能让访存代理继续随 CPU 频率加速。因此“工作集大于缓存”不能单独证明曲线应在高频变平；平台应以独立 Roofline 证据解释频点曲线，而不能给 compute/memory/mixed 标签反推一个计算强度。

最小可实施实验分两层，避免把完整强度阶梯叠加到每一个主曲线 OPP：

1. 每个核组先在一个代表 OPP 执行 compute ceiling、bandwidth ceiling 和 0.083 / 0.333 / 1.333 / 5.333 / 21.333 FLOP/B 强度阶梯，建立源级 `P_peak`、`B_source` 与交点；五档必须覆盖 `I_knee/2` 至 `2×I_knee`，所有点继续通过实际频率、落核、同胞静默、完整工作集和重复波动门禁。
2. 每个核组只在低、中、高三个 OPP 复测 bandwidth ceiling，得到 `B_source(f)`。若带宽屋顶本身随 CPU OPP 上移，memory 代理随频率改善就是可解释的设备事实；若 `B_source(f)` 已平坦而 memory 代理仍近似 `1/f`，则应优先检查标量循环、缓存层级和单线程未饱和，而不是修改拟合结果。
3. 需要真实 Instruction Roofline 时，复用校准 Worker 已有的同窗 PMU 起止合同，并从目标 SoC 的 uncore/DMC event source 补充读写 DRAM bytes。发布前需要核验事件归属、cache-line/beat 换算、计数溢出、复用缩放和与正式测量窗口的时间重合；缺任一项时保留为诊断代理。

经典 Roofline 是辅助解释模型，任务类型频点曲线仍以实测时延为寻优输入。前者回答“固定强度在计算/带宽屋顶的哪一侧”，后者回答“当前代理在这个核组和 OPP 上实际用了多久”；两者不相互覆盖，也不通过调整计算强度去强制制造预期的曲线形状。

## 落核与 OPP 控制

一键校准执行的是受管代理微基准，不是把应用中的真实 BuildItem 强制迁移到目标核。硬件画像为每个核组提供物理代表 CPU 和 cpufreq policy。执行一个“中核、最高 OPP”条件时：

1. 执行器从中核组选择一个独立物理核代表 CPU，并为观测线程选择另一物理核；
2. 目标 Worker 在线程内部调用 `sched_setaffinity` 绑定到单一 CPU，再用 `sched_getaffinity` 与 `sched_getcpu` 复核实际落核；
3. 执行器把中核共享频率域的 `scaling_min_freq` 与 `scaling_max_freq` 都写为该核组最高 OPP，并读回核验；
4. 微基准优先冻结目标逻辑 CPU 的 `cpuinfo_cur_freq`；以同负载分块预热，连续 3 次命中目标 OPP ±3% 后才打开测量窗口，正式窗口仍至少读取 5 次且完整范围位于 ±3%。初始文件不可用时记录 policy `scaling_cur_freq` fallback 供诊断，但拒绝拟合；
5. 正常完成、取消或异常退出都恢复原频率上下界。

锁定的是整个中核共享频率域，而不是某个线程的私有频点；per-CPU `cpuinfo_cur_freq` 用于证明工作线程所在逻辑 CPU 的实际频率响应。目标物理核同胞线程还要在同一目标窗口通过 `/proc/stat` 5% 忙占比门禁。HDC 只负责测量窗外的部署与启动，其往返时延不计入结果。若应用需要真实 BuildItem 的跨核/跨 OPP 回放，应另建可重入的阶段回放校准，显式控制输入、缓存初态和线程放置；代理曲线通过 PMU 负载分型映射真实阶段，不宣称代理就是应用阶段本身。

## 自动执行与拟合

```text
发现设备能力
  -> 读取 CPU 拓扑、核组、OPP 与频率控制权限
  -> 生成绑定设备画像的冻结计划
  -> 校验并部署内容寻址的受管微基准
  -> 按 核组 × OPP × 内核 执行稳定乱序实验
  -> 采集线程 CPU 时间、wall、实际频率、热区原始快照、落核与同窗 PMU 证据
  -> 过滤失稳样本并拟合参数
  -> 用未参与拟合的 OPP、工作规模、跨内核强度点与共跑工作量验证
  -> 检查来源完整性、统计质量与覆盖度
  -> 发布通过版本，或发布带失败门禁的 risk_calibrated 版本
```

频点不能锁定时，平台不能把目标 OPP 当作实际 OPP。当前执行器停止对应参数组，不发布频率模型；后续即使增加按实测频率分桶的观察模式，也不能把它当作受控频率实验。设备与计划的硬件画像、CPU 映射或 OPP 不一致时立即阻断，不能静默套用参考机配置。

拟合过程：

- 任务类型频点性能曲线先保存每个核组、类型和 OPP 的单位工作量时延中位数，再对训练点做连续分段拟合；命中曲线时按目标/观测条件的绝对曲线比值迁移。
- 计算和带宽屋顶直接用固定操作数或字节数除以有效测量时间，并保留重复区间。
- `f_knee/e_lo/e_hi` 对三类代理的实测 OPP 单位工作量 CPU 时间做连续分段约束拟合；经典 `P_peak/B` 用于解释物理交点，两类模型分开保存。
- 有向干扰系数使用相同条件下 `T_victim,corun / T_victim,solo - 1`，不对两个方向取平均。
- Worker 暖态派发使用相同目标核上的配对差分 `O_dispatch=(T_queue-T_direct)/N`；wall、调用方 CPU 和 Worker CPU 分开保存，线程池冷启动另列。
- 核型倍率、线性频率弹性和通用 `alpha` 只在主曲线或组合矩阵缺失时回退，并记录回退原因；不得与命中的主参数重复相乘。
- 留出集不参与参数估计；报告验证 MAPE、P90 APE、重复 CV 和适用域。

### 发布判定与风险记录

平台把“能否相信数据来自声明的实验”与“统计质量是否达到理想门槛”分开判定：

1. **结构与来源完整性是硬阻断。** 测量必须匹配冻结计划和硬件画像，声明真实测量来源，并通过 schema、数值、受管二进制 SHA-256、负载身份及条件校验。训练与验证不能混用二进制构建或完整运行 ID；同一频响曲线和经典 Roofline 的目标 CPU、观测 CPU、工作集必须保持一致；共跑训练与验证必须落在相同物理放置域。任一来源完整性错误进入 `publication_blockers`，模型不可发布。
2. **完整通过。** 至少有可用实测和估计参数，且来源完整性、重复稳定性、训练残差、独立留出误差及条件覆盖均通过时，发布正常校准版本；`risk_level=low`、`evidence_grade=A`。
3. **带风险发布。** 来源完整性通过且已有可用实测和至少一个估计参数，但重复 CV、训练/留出误差等统计质量门，或 OPP、重复数、留出条件、参数组等覆盖门未通过时，拟合状态为 `publishable_with_risk`，发布版本状态为 `risk_calibrated`。只有质量门失败时为 `risk_level=medium`、`evidence_grade=B`；存在覆盖缺口时为 `risk_level=high`、`evidence_grade=C`。
4. **风险可追溯。** 拟合与模型版本保留 `risk_level`、`risk_summary`、`evidence_grade` 和逐项 `failed_gates`；`failed_gates` 区分 `quality` 与 `coverage`，不把失败项压缩成一个布尔值。风险校准模型可以进入并行寻优，界面必须在拟合结果、已发布版本和寻优模型选择处持续展示风险，不能伪装成质量通过版本。

PMU 事件部分或全部不可用时，只把不可用事件和派生指标记为 `null`，不把这一事实单独列为时延拟合的来源完整性阻断。时延样本仍需独立满足落核、实际频率、计时、二进制与实验条件合同；PMU 缺失只限制微架构解释范围。

## 页面交互

页面首先展示“校准什么、用什么微基准、如何拟合、怎样验证”。默认工作流只有一个主入口：选择本机 HDC 设备后点击“在真机执行并自动校准”。运行中显示当前阶段和实验进度，运行记录保留条件、实际频率、落核、同窗 PMU 与热区原始证据。完成后自动拟合并展示留出误差；质量通过时发布正常模型，统计质量或覆盖未通过但仍可发布时，按钮和版本卡明确标识“带风险保存”和风险校准状态，并允许用户选择该模型寻优。

手工导入或导出 JSON 放在“高级与恢复”区域，用于迁移已有实测和排查故障，不要求普通用户制作测量文件。

## 参考依据

- Williams、Waterman、Patterson，*Roofline: An Insightful Visual Performance Model for Multicore Architectures*：<https://www2.eecs.berkeley.edu/Pubs/TechRpts/2008/Archive/EECS-2008-134.pdf>
- Instruction Roofline：<https://escholarship.org/uc/item/7q73n52w>
- STREAM memory bandwidth benchmark：<https://www.cs.virginia.edu/~mccalpin/stream/>
- lmbench `lat_mem_rd`：<https://lmbench.sourceforge.net/man/lat_mem_rd.8.html>
