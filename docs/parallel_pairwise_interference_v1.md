# 双任务负载组合干扰校准

状态：Normative v1.0；校准 producer 1.2.0；适用于当前服务依赖 DAG 模型。单任务核频语义见 [并行模型](parallel_modeling_model_v1.md)，Trace 与版本管理见 [校准工作台](trace_storage_and_calibration.md)。

## 两部分模型

单任务模型将 Trace CPU 参考工作换算到目标核与频点：

`solo_cpu_ms = W × R_kind × [(1−e_kind) + e_kind × max_khz/f_khz]`。

共跑校准测量“目标代理 A 在代理 B 持续执行时的线程 CPU 膨胀”，用方向性系数：

`beta(A←B) = median(cpu_A_corun / matched_solo_cpu_A − 1)`。

匹配单跑按相同目标物理核、工作集、核组与频点，CPU 时间按工作量归一。A←B 不与 B←A 合并。模型只表征非负干扰：−2% 内的负值按噪声归零，更低负值或大于 5 的 beta 不通过模型适用域检查。代理类别 compute/memory/mixed 指实验内核，不证明应用阶段的缓存层级或 DRAM 带宽瓶颈。

## 冻结方案与数据合同

沿用 `/v1/parallel-calibration/plans|fits|versions`，新增 `parameter_groups: ["pairwise_interference"]`，也可与单任务校准组同时选择。

方案可提供：

```json
{
  "parameter_groups": ["pairwise_interference"],
  "interference_placements": [
    {"victim_core_kind": "middle", "peer_core_kind": "middle", "victim_frequency_khz": 2151000, "peer_frequency_khz": 2151000}
  ],
  "pair_work_units": 100000,
  "working_set_bytes": 67108864,
  "repeats": 5
}
```

上述频点仅展示字段结构；提交前使用 `/catalog` 中实际 OPP。省略频点时选择对应核组最大 OPP，默认放置中核+中核。当前参考设备只有一个大物理核，不支持大核+大核。同核组共享 OPP，双方频率必须相同。每个放置生成三类单跑、九类有向共跑以及双倍工作量独立验证条件，工作集保持不变。

`parallel_calibration_measurements/v1` 兼容扩展；pair_work 样本增加：

| 字段 | 语义 |
| --- | --- |
| benchmark / family | `pair_work` / `build` |
| workload_class | `compute`、`memory`、`mixed` |
| peer_workload_class / peer_core_kind | 干扰者类别/核组；单跑均为 `none` |
| peer_frequency_khz | 干扰者配置频率；单跑 0 |
| working_set_bytes | 每线程工作集，冻结方案值 |
| cpu / helpers[].cpu | 实际目标与干扰者逻辑 CPU，必须与 provenance.cpu_map 一致且核验独立物理核 |
| peer_frequency_verified | 干扰者频率区间核验 |
| peer_observed_frequency_khz / peer_frequency_min_khz / peer_frequency_max_khz | 干扰者频率均值与范围，均需在目标 ±5% |
| observer_cpu / observer_independent_core | 固定采样CPU编号及独立物理核核验；单跑/共跑/验证保持相同 |

沿用设备、OS/固件、二进制 SHA-256、输入哈希、独立运行 ID、训练/验证划分、线程 CPU/wall、落核、目标频率、温度及共跑覆盖证据。明确 synthetic/fixture 标志不接受发布。服务检查输入一致性，不独立证明外部导入者的设备身份或测量真实性。

每条件至少三次训练、两次独立验证；重复 CV ≤15%，每个留出条件及全体验证 median APE ≤10%、P90 ≤20%。缺单跑锚点、训练验证运行泄漏、缺干扰者频率、跨二进制、改变目标/干扰物理放置均不通过。只有全部所选参数通过实测与留出门槛的 fit 才能保存模型版本。

## 可执行代理与采集

校准包保留原有 `benchmark.c`、`collect.py`、方案、空测量模板和说明。二进制 producer `parallel-calibration-proxy-v2` 新增三种内核：

- compute：整数混合依赖链，结果汇入可观测 sink。
- memory：每线程独立固定工作集，按连续8 KiB块顺序 volatile 读写，块边界回绕、四路独立累加，避免逐元素整数除法。
- mixed：每单位交错执行整数混合与相同顺序访存。

内存初始化和 warmup 在计时前完成。目标每轮运行 `min(work_units, 32768)` 个同类型工作单元，再读取已冻结到目标逻辑 CPU 的 `cpuinfo_cur_freq`；连续 3 次落入目标 OPP ±3% 后开始正式窗口，最多 12 轮，实际轮数、频率范围、末次读数和累计 `warmup_work_units` 全部落盘。正式窗口仍至少采 5 次，且 target 与 helper 分别使用自身 per-CPU 路径；初始文件不可用时记录 `scaling_cur_freq_fallback` 供诊断，但该样本不能拟合。计时使用 `CLOCK_THREAD_CPUTIME_ID` 和单调 wall。干扰者覆盖目标整个区间；验证 CPU 在目标区间的保守占用下界 ≥90%。采集器从整个冻结方案的目标/helper并集之外选择固定observer，分批筛选不会改变其位置；没有空闲核时可显式指定额外独立物理核。单跑/共跑/留出采样CPU改变将使配对锚点失配，不能发布。采集器支持 `--benchmark pair_work` 和 `--peer-core-kind` / `--peer-frequency-khz` 选择受外部频率控制的一批条件，`--dry-run` 只产生命令，无测量。

这些内核及其固定工作集是代理，不自动等价于 ArkUI BuildItem。选择工作集时需结合设备缓存容量与阶段特征；“memory”不保证该设备 DRAM 已饱和。此变更仅做了编译与隔离数值合同测试，尚无目标真机校准证据。

## 模型发布与寻优消费

版本 `model.interference_model` 的 schema 为 `directional_pairwise_interference/v1`，含状态、版本、硬件 ID、来源与测量放置、`active_tasks_domain:[2]`、`cells`。

每个 cell 保存 `victim_class` / `peer_class`、双方核组与频点、beta、工作集、实际双方 CPU、测量工作量及样本数。只有 `status=proxy_holdout_validated` 的矩阵能消费。未校准的单任务倍率、频率弹性、alpha 和启动开销继续明确标为继承参数。

模拟读取 `graph.component_classes[component_id]` 或节点 `workload_class`。每次离散事件：

1. 统计所有活动应用 CPU 工作，包括主线程和 Worker 启动。服务端 CPU 与等待不计入活动数。
2. 目标为 BuildItem 且存在另一活动任务时，匹配双方类别、核组、频点和 N=2。
3. 精确匹配 cell 时，CPU 速率为 `1/(R × F × (1+beta))`；否则沿用 `1/(R × F × (1+alpha×(N−1)))`。
4. 一个任务结束或进入等待后，立即重算活动组合与剩余参考工作。任务依赖、服务通道、启动开销保留原逻辑，最终预测 UI 回调结束，不是呈现帧率。

未知类别、主线程/启动无类别、未测频点/组合以及三个以上活动 CPU 任务均可触发回退。不同类的两两 beta 不未经验证相加为多任务模型。核组匹配是代理迁移；具体物理核心位置和共享缓存拓扑相同尚未证明，随结果明确报告。

`result.interference` 保存矩阵版本、匹配 cell、`calibrated_cpu_ms`、`fallback_cpu_ms`、各回退原因的 CPU 时间和状态；时间是目标 BuildItem CPU 活动区间的累积，不是整个回调 wall 覆盖率。原实测 baseline replay 使用 contention=false，任何校准与阶段类别均不修改基线。

## 验证与兼容

旧 v1 测量与 alpha 模型继续使用；原 API schema 外层保持 v1，仅扩展可选组和字段。模型 producer/版本现为 1.2.0，新增逐核组/逐负载类型 frequency-Roofline 拐点；旧版本文件不重写。

`backend/tests/test_parallel_interference.py` 验证方向性拟合、发布适用域、负例质量门槛、样本顺序不变性、单任务/共跑联合校准、类别驱动预测、事件后恢复单跑速率和未知/高并发回退。测试数值仅存在临时状态，不作为用户可用的设备校准版本。
