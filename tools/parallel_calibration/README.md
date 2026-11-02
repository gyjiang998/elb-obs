# 离线代理微基准

此包针对所下载 `plan.json` 采集真实线程 CPU 数据，拟合现有条件成本模型。它只校准 On-CPU 的核型、核频与共跑响应。应用 Trace 的 Off-CPU 汇总保留为观测账目：非 Binder delay 节点在预测图中保留实测时长，同步 Binder blocked 由 send → service → reply → wake 依赖链表达；两者都不由本包缩放，汇总值也不能直接加到预测 On-CPU 上。它不执行图库代码，不验证组件线程安全，也不把微基准结果当作图库并行收益。采集器不调用 HDC / ADB、不写 cpufreq、不执行提权命令、不发布模型。

## 编译与设备准备

在目标设备 ABI 的工具链中编译：

```sh
cc -O2 -std=c11 -pthread benchmark.c -o benchmark
```

HarmonyOS / Android 应使用对应 SDK 的 C 编译器、sysroot 与 pthread 支持，再按现有设备管理流程放到目标设备。`collect.py` 需要在测量机器上运行 Python 3.10+；没有 Python 的设备可使用 `--dry-run` 导出的命令逐条运行 `benchmark`，但需按 `measurement-contract.json` 包装真实结果并保留独立 run_id。下载文件本身不会操作设备。

默认物理核代表为 big `[10]`、middle `[4,6,8]`、small `[0,1,2,3]`。目标设备需先核对这些逻辑 CPU 的实际类型、SMT 关系与 OPP；用 `--cpu-map cpu-map.json` 覆盖映射。程序读取 `core_cpus_list` 与 `thread_siblings_list`，并要求每个物理核组的所有成员都公布完全一致、包含自身的双向映射；两类映射同时存在时还必须彼此一致。任一文件存在但格式非法、只在一侧存在、遗漏成员或非对称时，程序返回拓扑冲突并拒绝执行，不会改用另一份列表或 `physical_package_id/core_id` 猜测。只有两个列表机制对被比较 CPU 都完全不存在时才检查 `physical_package_id/core_id`：相同 ID 足以判定两个逻辑 CPU 不能作为独立物理核，不同 ID 仍只记为未知，不能证明它们相互独立。这是兼容路径，因为部分 Kirin 内核会为同一物理核的两个逻辑线程返回不同 `core_id`。程序同时核验线程单 CPU 亲和性，并为协调 / 采样选择单独物理核。若 sysfs 或亲和性不可用，结果明确不合格，不能据此发布校准版本。

## 分批采集与合并

先查看计划命令，不会产生实测样本：

```sh
python3 collect.py --plan plan.json --binary ./benchmark --output commands.json --dry-run
```

使用既有平台或设备管理手段控制实际频率、热状态和后台负载后，按核组 / OPP 分批运行。以下仅演示命令格式，设备标识、系统版本与热状态说明必须改成实际值：

```sh
python3 collect.py --plan plan.json --binary ./benchmark --output big-max.json \
  --core-kind big --frequency-khz 2500000 \
  --device-id DEVICE_ID --os-build ACTUAL_OS_BUILD \
  --build-info 'TARGET_COMPILER -O2 -std=c11 -pthread; ACTUAL_ABI' \
  --thermal-state normal --thermal-note '记录实际监测手段与观察结果'
```

选择另一个冻结 OPP 时，先在外部配置并确认设备状态，再执行相应批次。每个条件按计划 repeats 启动独立进程，train 与 validation 使用独立 run_id。可用 `--condition-id`、`--benchmark`、`--family`、`--split` 进一步筛选；不会修改方案的 work_units 或数据划分。`--timeout-seconds` 默认 300 秒，`--cooldown-seconds` 默认 1 秒，可根据工作规模和温度恢复情况调整。

```sh
python3 collect.py --plan plan.json --output measurements.json --merge-only \
  --merge big-max.json --merge big-low.json --merge big-middle.json \
  --merge middle-max.json --merge middle-low.json --merge middle-middle.json \
  --merge small-max.json --merge small-low.json --merge small-middle.json
```

同一合并集必须使用同一冻结计划、设备、OS、binary SHA-256 和物理核映射。多个批次不是重复行：每条样本有全局唯一 sample_id、独立运行 run_id、条件 / repeat / split、原始 stdout 哈希及实际命令。输出在每次测量后更新；失败和无效测量保留诊断。退出码 2 表示条件 / 测量不完整，不会伪装成功。

## 实际测量内容

- `object_tree_v1` 使用固定大小的 63 节点连续对象树，反复设置属性、连接子节点、遍历并消费结果；是可重现的对象树 CPU 代理。
- `integer_mix_v1` 使用整数依赖链与小型线程局部状态，独立拟合其他 CPU 工作的核组迁移参数。
- `pair_work` 提供 compute（整数依赖链）、memory（固定工作集顺序读写）、mixed（计算/访存交错）三种内核，目标与 helper 可以选择不同类型。每线程初始化与 warmup 在计时前完成，工作集随方案冻结。目标线程每轮执行 `min(work_units, 32768)` 个同类工作单元（`empty_dispatch` 为 32 个同 payload 任务），随后读取同一个已冻结频率源；连续 3 次落入目标 OPP ±3% 才打开正式测量窗口，最多 12 轮。`warmup_work_units` 保存实际累计工作量，九项 `warmup_frequency_*` 字段保存收敛证据，均不计入目标时延。类别是代理结构，需结合实际工作集/缓存容量解释，不直接等价于应用阶段或 DRAM 带宽饱和。

  v6 的三类代理还冻结了 `pair_work_source_intensity/v1` 源码强度合同。compute 每 work unit 调用 64 次 `mix64`，按 payload 源码表达式计 640 次整数算术/位操作，没有可作为分母的显式流式缓冲区字节，因此整数运算强度保持 `null`，不能写成无穷大。memory 每 work unit 顺序处理 1024 个 `uint64_t`，最少读取 8 KiB、写入 8 KiB并计 2052 次整数 payload 操作，约为 0.125244 源码整数操作/最小源码 Byte；mixed 在相同 16 KiB 最小读写量上再执行 64 次 `mix64`，共 2692 次操作，约为 0.164307 操作/Byte。上述操作数排除循环、索引、地址生成和控制开销；字节数排除 cache line、write allocate、预取和实际 DRAM 流量，均不等于 FLOP、retired instructions 或 PMU DRAM bytes。

  memory/mixed 每个 work unit 只将工作集游标推进 8 KiB，到工作集末尾后回绕；方案同时保存 `working_set_bytes`、总推进量和完整遍历次数。64 MiB 工作集通常大于单核缓存，但这本身不能证明每次访问到达 DRAM，也不能证明内存带宽已经饱和。若缓存/互联/内存控制器频率与 CPU OPP 联动，或者内核仍受整数加法、循环、地址生成、load/store 发射率和数据依赖限制，memory 曲线仍可能近似随 CPU 频率变化。判断真实计算强度应另采同窗 PMU retired instructions 与经验证的 DRAM bytes；缺少后者时不发布 Instruction Roofline。
- 共跑时，目标线程处理固定 work_units，N−1 个 helper 在其他物理核运行同类代理直至目标完成。helper 优先用同核组剩余物理核，再按默认映射选其他核；实际 CPU、线程 CPU、起止时刻随样本保存。α 只覆盖此干扰组合，不能当成任意背景负载的已验证系数。
- `empty_dispatch_v1` 在同一个持久 worker、目标 CPU 和 OPP 上做配对测量。`direct_batch` 路径在 worker 内连续执行 N 次 `mix64_v1` 固定 payload；`queue_dispatch` 路径由 producer 逐项投递相同 task ID 和初始 seed，并逐项等待确认。两条路径的最终 sink 必须相等，避免把不同有效工作误算成投递开销。结果分别保存 worker CPU、producer CPU 和 producer 观察到的往返 wall 总量，并输出 `(queue − direct_batch) / task_count` 的三种每任务差分。

  兼容字段 `cpu_ms`、`wall_ms` 和 `worker_cpu_ms_per_task` 仍表示逐项队列路径的 worker 侧成本，供旧拟合器读取；新的主证据是 `dispatch_wall_delta_ms_per_task`、`dispatch_worker_cpu_delta_ms_per_task` 与 `dispatch_producer_cpu_delta_ms_per_task`。线程在两条路径开始前已经创建、绑核并完成 warmup，因此 `cold_start_included=false`：本实验不包含 `pthread_create`、绑核、ready 握手、join、复制、GC 或冷缓存，不能命名为线程冷启动开销。

  三个配对指标分别按 `（逐项队列总量 − worker 内批量直执行总量）/ task_count` 计算：wall 使用 producer 观察到的完整开始—完成往返区间，worker CPU 使用目标 worker 的 `CLOCK_THREAD_CPUTIME_ID`，producer CPU 使用投递线程的 `CLOCK_THREAD_CPUTIME_ID`。总量和差分同时落盘，Desktop 会复算差分；任一总量缺失、sink 不一致或差分无法复算时，该 v4 样本不进入拟合。

共跑不只检查线程是否启动。`concurrency_verified=true` 需要目标 cpu_work 的 CPU / wall ≥90%，每个 helper 的时间区间完整覆盖目标区间，helper 总 CPU / wall ≥90%，并且其目标窗口内 CPU 占用率下界 ≥90%。下界为 `max(0, helper_cpu_ms − helper 在目标窗口外的 wall_ms) / target_wall_ms`；保守地扣掉全部窗口外时间，避免前后额外工作掩盖目标窗口内被抢占。每个 helper 保存 `covers_target_interval/cpu_busy_fraction/overlap_cpu_lower_bound_ms/overlap_cpu_lower_bound_fraction`，可以独立复算。任何 helper 覆盖不足，采集器都将 active_tasks >1 样本列为无效，后端不得把它当作持续 N 任务共跑。sysfs 已确认目标 / helper 共用同一物理核时，程序在创建测量线程前拒绝执行；sysfs 缺失仍标为未验证。

频率证据优先绑定工作线程实际逻辑 CPU 的 `/sys/devices/system/cpu/cpuN/cpufreq/cpuinfo_cur_freq`。基准在预热前选择一次来源，预热和正式窗口始终读取同一路径，读失败也不切换。只有初始 `cpuinfo_cur_freq` 不可用时才冻结到 policy 的 `scaling_cur_freq`，并明确记录 `scaling_cur_freq_fallback`；该结果只用于诊断，不能进入生产拟合。正式窗口默认每 1 ms 采样、至少 5 次有效读数且无读取失败，完整 min/max 必须在目标 OPP ±3% 内。均值、min/max、采样数、读取失败数、来源 CPU、完整路径与起止时间全部保留。peer 频率同样绑定实际 helper CPU。sysfs 值仍是内核报告频率，不代表独立硅片测频。

新生成的 `plan.json` 还会冻结每个核组的 `frequency_policy_ids`。采集命令将它作为 `--frequency-policy-id policyN|cpuN` 显式传给微基准，用于锁频恢复和 cpuinfo 不可用时的诊断回退；双任务跨域共跑还会传入对端 policy。生产频率证据仍必须来自目标/helper 各自的 per-CPU `cpuinfo_cur_freq`。若目标系统无法提供该文件，应保留 fallback 原始证据并停止拟合，不能手工改写冻结计划或用 policy 文本冒充实际逻辑 CPU 频率。

## 任务类型频点性能曲线

选择 `frequency_roofline` 参数组时，冻结方案为每个所选核组生成 compute、memory、mixed 三类单任务频率扫描。每类总计至少包含 **6 个不同真实 OPP**：至少 5 个训练 OPP，另 1 个完全不参与拟合的独立留出 OPP。方案强制包含硬件最低和最高 OPP，并将两者放入训练集；默认从完整 OPP 表选取 6 个覆盖最低频、最高频及中间区间的位置，再把中间位置单独划为留出 OPP。若目标硬件少于 6 个不同 OPP，该核组不能发布此模型；若需要自定义扫描点，应在生成方案时显式提供频点，不能在采集 JSON 中自行改写冻结条件。

采集方式仍按核组/OPP 分批执行，例如：

```sh
python3 collect.py --plan plan.json --binary ./benchmark --output middle-opp.json \
  --benchmark pair_work --core-kind middle --frequency-khz 1800000 \
  --device-id DEVICE_ID --os-build ACTUAL_OS_BUILD \
  --thermal-state normal --thermal-note '记录实际监测手段与观察结果'
```

`plan.json` 的 `core_kinds` 记录通用校准核组，`frequency_roofline_core_kinds` 只记录其中至少具备 6 个不同 OPP、可执行频点曲线校准的核组；低 OPP 核组只跳过曲线，不缩小其他参数组的范围。`frequency_roofline_split` 必须与该子集键集合完全一致，并分别记录 `train_khz` 与 `validation_khz`。同一 OPP 批次会依次运行三种 `workload_class`，也可用 `--condition-id` 精确采集单个冻结条件。每个训练 OPP 使用方案指定的工作量和至少 3 个独立 `run_id`；留出 OPP 使用相同类型、不同工作量和至少 2 个独立 `run_id`，不能从训练运行复制、拆分或参与参数拟合。

拟合只读取 `train` 条件：先按核组、类型和训练 OPP 汇总单位工作量线程 CPU 时间中位数，并以训练集中的最高硬件 OPP 为 1。候选拐点只来自训练 OPP；拐点两侧拟合连续分段幂律。`validation` OPP 不进入上述中位数、候选网格或指数拟合，只在模型固定后检查预测误差：

```text
G(f) = (fmax/f)^e_hi                                      f >= fknee
G(f) = (fmax/fknee)^e_hi × (fknee/f)^e_lo                 f <  fknee
0 <= e_hi <= e_lo <= 1
```

`e_lo` 表示拐点以下的频率响应，`e_hi` 表示拐点以上仍保留的响应。计算代理通常在高频仍对频率敏感；访存代理可能较早进入平台区；混合代理介于两者。这是借鉴 Roofline 形态、以 CPU 频率为横轴的 frequency analogue；经典 Roofline 以运算强度为横轴，两者不是同一个公式。该分段曲线直接替代旧 `frequency_elasticity`，不能把两者相乘。

发布曲线的 `frequency_domain_khz` 只包含训练 OPP；运行时仅在 `[min(train_khz), max(train_khz)]` 闭区间内使用曲线。频率不必正好等于训练点，留出 OPP 正是用于验证未见中间点的插值能力。硬件 min/max 已进入训练，因此同一硬件 OPP 目录正常落在域内。旧模型缺少合法训练域、阶段类型未知、某类曲线缺失或请求频率超出闭区间时，寻优明确回退旧弹性，并在结果中记录回退原因，不使用分段公式外推。

应用阶段的 compute / memory / mixed 来自 PMU 特征规则，始终只是候选分类。它用于选择哪条已经实测的代理曲线，不会把一次 PMU 分类变成拐点；通用 cache miss 也不能代替 DRAM 字节、实际带宽或经典 Roofline 运算强度。代理类别、工作集、缓存层级与目标应用阶段的等价性仍需单独验证。

## 经典 Roofline 实测

`roofline` 模式与上一节的核频—时延分段函数是两套不同模型。经典 Roofline 在横轴使用运算强度 `I`（FLOP/Byte），纵轴使用可达性能（FLOP/s）：

默认一键校准当前采集任务类型频点曲线、双任务干扰矩阵和 Worker 派发开销，不自动附加经典 Roofline 的大流量强度阶梯。因此仅看到 compute/memory/mixed 频点曲线时，不能反推浮点计算强度；需要在方案中显式选择 `classic_roofline` 才会采集下列 compute ceiling、bandwidth ceiling 与 intensity ladder 条件。这一选择独立增加测量量，不改写已经发布的 v6 频点曲线。

```text
P_attainable(I, f) = min(P_peak(f), BW_sustainable(f) × I)
I_knee(f) = P_peak(f) / BW_sustainable(f)
```

平台应对每个核组、每个真实 OPP 独立采集以下固定内核。计算与带宽屋顶至少重复 3 次且变异系数不超过 5%；每个强度点至少重复 2 次且变异系数不超过 8%。计算屋顶使用 64,000,000 work units，访存路径累计处理至少 1 GiB 源码最小字节量，且目标线程 CPU 忙占比必须达到 95%，避免把数毫秒冷启动或调度噪声当成平台屋顶：

- `compute_ceiling`：16 条独立双精度乘加递推，每次 work unit 有 32 个源码级 FLOP，用于测量该编译产物在目标核上的计算屋顶。
- `bandwidth_ceiling`：固定大工作集上的双读、单写流式内核，每元素最少 24 Byte、2 个源码级 FLOP，用于测量可持续的源码数据带宽屋顶。
- `intensity_ladder`：`classic-roofline-blocked-v2` 在 16 个独立元素上使用 `value = value × multiplier + source` 双精度递推，保持同一工作集和 24 Byte/元素，默认依次使用 2、8、32、128、512 个源码级 FLOP/元素；任意 2–4096 的偶数值也走相同 16 路 ILP 内核，可围绕实测拐点补点。阶梯必须至少包含 5 个强度点，覆盖 `I <= I_knee/2` 与 `I >= 2×I_knee`，最高强度至少达到计算屋顶的 75%；平台还会检查实测点不显著越过 `min(P_peak, BW×I)`、吞吐不随强度反常下降，并用 2-FLOP 点交叉核验带宽屋顶。远低于屋顶仍可能来自内核自身的其他瓶颈，因此不以“贴近屋顶”作为通过条件。
- `instruction_proxy`：固定整数混合轮次和数据访问，仅作为 BuildItem 类整数/控制负载的可重复代理。它输出的是 `integer_mix_rounds`，绝不把源码运算数伪装成 retired instructions。

示例命令如下；正式方案由平台按设备拓扑与 OPP 生成，当前使用冻结的 64 MiB 大工作集并至少完整扫过数次。平台尚未探测 LLC 容量或拆分缓存层级屋顶：

```sh
./benchmark --benchmark roofline --family roofline \
  --cpu 4 --observer-cpu 0 --frequency-khz 1800000 \
  --frequency-policy-id policy4 \
  --workload-class intensity_ladder --roofline-operations-per-element 32 \
  --working-set-bytes 67108864 --work-units 11184808 \
  --active-tasks 1 --sample-period-us 1000 --frequency-tolerance 0.03
```

程序在计时前完成固定输入初始化和 warmup，输出 `algorithmic_flops`、`algorithmic_bytes_minimum`、`attainable_gflops`、`source_level_gbytes_per_s` 与工作集覆盖次数。`algorithmic_flops` 可由源码复算，但可能被编译为 FMA 或 SIMD；它不是硬件 retired FP 指令数。`algorithmic_bytes_minimum` 只含两次源码读取与一次源码写入，不含 write allocate、cache refill 或预取流量，因此也不冒充内存控制器 DRAM 字节。

BuildItem 主要是整数、分支、指针和内存访问，应用 **Instruction Roofline**，不应把它套进 FLOP Roofline。发布 Instruction Roofline 前，平台必须在同一计时窗口对齐采集 `retired instructions` 和经验证的 DRAM byte 事件；据此计算 instructions/s 与 instructions/byte。若设备 PMU 缺少 DRAM 流量事件，页面只展示整数代理和源码级字节数据，并明确标记“缺少对齐 PMU 证据”，不能给出伪造的 Instruction Roofline。

改变 CPU 频率通常会移动 `P_peak(f)`，带宽屋顶是否变化需要按 OPP 实测，不能假定不变。对固定运算强度的负载，较低频率可能由计算屋顶限制；频率升高后计算屋顶上移，负载点可能转为带宽屋顶限制。这里变化的是平台屋顶及 `I_knee`，负载算法本身的运算强度不会因拖动频率滑块而自动改变。

## 负载组合共跑

选择“负载组合干扰矩阵”后，方案生成三个单跑类型、九个有向两任务组合以及独立双倍工作量验证条件。单跑与共跑固定同一目标核、频点、工作集。以下默认参考中核最大频点示例按 `plan.json` 调整：

```sh
python3 collect.py --plan plan.json --binary ./benchmark --output pair-middle.json \
  --benchmark pair_work --core-kind middle --frequency-khz 2151000 \
  --peer-core-kind middle --peer-frequency-khz 2151000 \
  --device-id DEVICE_ID --os-build ACTUAL_OS_BUILD \
  --thermal-state normal --thermal-note '记录实际监测手段与观察结果'
```

同核组选择不同物理核并使用共享 OPP。跨核组时分别控制双方 OPP；采样器核验双方的 min/max 与频率采样，版本保留具体 CPU 放置。采集器从整个冻结方案的目标/helper并集之外选择固定采样CPU，单跑、共跑与各批次保持一致；没有空闲核时可指定额外独立物理核 `--observer-cpu`，后端按相同observer匹配单跑锚点。helper 持续执行指定代理，目标完成后停止；分别标定 A←B 与 B←A。版本保存有方向的 CPU 膨胀，不把两个方向平均成一个“并行度”。

二进制版本为 `parallel-calibration-proxy-v6`。v6 保留 v4 的同 payload Worker 配对差分和 v5 的同胞线程静默证据，并新增 per-CPU 实际频率来源及自适应驻留预热合同。基准在目标计时窗口前后读取 `/proc/stat`，对目标物理核的其余逻辑线程逐一保存 busy/total tick 差分，取最坏 busy fraction，以 5% 为硬质量门。拓扑必须来自完整、互相一致的 `core_cpus_list` / `thread_siblings_list`；独立物理核没有同胞逻辑线程时记录空列表、busy=0 且核验通过。拓扑、计数器、per-CPU 频率或预热驻留证据缺失，以及同胞线程忙占比或实际频率超限，都会使当前尝试失效。Desktop 对同一个冻结 repeat 最多执行 3 次，补测触发条件为同胞干扰或 target/peer 实际频率未通过；每次原始尝试及原因均保留，三次仍失败则 repeat 未完成并阻断拟合。v4/v5 只保留历史诊断兼容，不可发布新模型。Desktop 同时核验 `paired_dispatch_delta/v1` 的身份、payload 一致性、总量及每任务差分。`--dry-run` 可以先输出异类组合的实际命令；校准包的空模板从不填入示意时延。

寻优仅在双方类别、核组、频点和两个活动任务匹配时使用该矩阵；未知类型、未测条件或三个以上活动 CPU 任务沿用 alpha，并报告回退覆盖。按核组迁移到应用仍需检验具体放置、共享缓存和工作集差异。

热状态默认 `unknown`，因此不能通过拟合质量门禁。`--thermal-state normal` 必须配套 `--thermal-note`，表示操作者实际监测后的声明；另保留运行前后的 thermal-zone 原始值。仅频率平稳不能证明没有热约束，程序不会自动作此推断。若无法确认状态，保留 unknown 并补采。

## 质量与边界

校准前至少核对：同一工作版本 / work_units / 编译 binary、设备和 OS；真实落核及物理核独立；目标物理核其余逻辑线程在目标窗口内静默且证据完整；频率持续覆盖；目标线程 CPU ≥1 ms；每类/每核至少 5 个训练 OPP 与 1 个未见留出 OPP；训练重复 ≥3 次；验证重复 ≥2 次；温度 / 背景状态；重复离散度；训练 / 留出条件和 run_id 独立。硬件 min/max 覆盖、最大 OPP 锚点、留出 OPP、工作规模和并发留出均由冻结 plan 定义。

后端再检查缺项、频率偏差、重复 CV、留出误差和模型适用域。缺数据、未核验或不符合当前公式的数据不能通过填入默认值获得“校准通过”。微基准发布版本仍属于代理迁移模型，应用级正确性与收益需单独测量。

生成的 `measurements.json` 可在 Web 或 Desktop 的「寻优校准—实测与拟合」导入。Desktop 接受不超过 20 MiB 的 UTF-8 JSON，通过一次性预约、SHA-256 校验和 target/profile 绑定的 typed SFTP 暂存合同送入同一拟合引擎；文件内容不经过 SSH 命令参数。传输和拟合结束后会清理暂存文件，服务端持久化的是拟合记录与模型版本。
