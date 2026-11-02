# 比较与诊断规则

## 先判定可比性

每项指标使用 `comparable`、`partial`、`not_comparable` 或对应分析器状态，并附理由。可比至少要求相同语义、目标范围与负载口径；硬件、CPU 数量、build 或采样策略变化时明确限制。节点数与每节点流量同时变化，不能把所有差异归于“节点扩展”。

`baseline` 是单节点 Guest，`scaled` 是 16 节点部署中实际被采样的 Guest 集合，`host_single_node` 是基线 Host 佐证。只有观测了全部节点，或明确声明并验证代表性假设，才形成集群总量。不能默认 `单个 Guest 的 PPS × 16` 是实测集群吞吐。

## 量、公式与分母

| 指标 | 允许的计算及限制 |
| --- | --- |
| perf Self/Children 占比 | 保留原列与分母范围；比较用百分点 `scaled_pct - baseline_pct`。Children 可重叠，不能加总成总开销。局部 callgraph 百分比不等于全局百分比。 |
| 原始样本 `n` | 总是可展示为观测量；跨 run 的原始数量不能直接表示函数变慢。完整性、频率/period 策略、事件、CPU/TID 范围必须一起保留。 |
| `samples/s = n / duration_s` | 时长可信且采样策略相同/可校准时才用于开销比较；否则只能标为采集样本速率，不能解释为运行成本。 |
| 不同频率采样 | 仅在目标/事件一致、频率机制已知且无明显丢样本或限频时，可给出 `n / (duration_s × frequency_hz)` 的归一采样指标；标记估算，不能当作精确 CPU 利用率或 event count。固定 period 采样不可套用此式。 |
| period 加权占比 | 若有完整且口径相同的 `period_sum`，`period_sum / duration_s` 可作事件规模估计；period 的单位依事件而定，不能默认是纳秒。`overhead × total_samples` 只有确认占比按样本次数计算且分母完整时才是近似数量，仍受舍入影响。 |
| perf stat counts | 区分原始和已缩放；仅对尚未缩放的原始值且 `time_running > 0` 使用 `raw × time_enabled / time_running`。低运行比例降低可信度，缩放无法恢复丢失的负载时序。 |
| IPC / MPKI | 同范围、同窗口且事件有效时：`instructions / cycles`、`misses × 1000 / instructions`。通用 cache-misses 不自动变成 LLC misses；不同微架构事件定义未核验不直接同比。 |
| 每包成本 | 同目标/时间窗的 `cycles / packets`、`instructions / packets`、`misses / packets`；缺业务包计数时留空。不能把 cycles 采样个数直接当 cycles。 |
| CPU 占用 | 同字段定义、同 CPU/线程范围比较；sar `all` 是整体视图，不是某个 worker 的利用率。占用变化不能独立证明调度抢占或处理成本变化。 |
| 吞吐扩展 | 若有同口径实测集群总吞吐，可算 `speedup = T16 / T1`；只有工作负载和资源扩展定义支持时才算 `efficiency = speedup / 16`，不能用于声称固定总负载应得到 16 倍吞吐。 |
| 相对变化 | `scaled / baseline - 1` 仅在分母非零且值可比时计算。基线为零时报告绝对变化/新增，不输出无穷倍或伪造百分比。 |

**占比反转示例（合成数据）**：同频、同事件、同 scope，baseline 10 s 收到 1000 个样本，其中函数 f 为 200 个（20%）；scaled 40 s 收到 8000 个样本，其中 f 为 800 个（10%）。f 的占比降低 10 个百分点，样本数增至 4 倍，但样本速率同为 20/s。仅此不能说 f 降本一半或变慢 4 倍。若 PPS 缺失，每包成本仍未知。

## CVS / DPVS 诊断边界

- 按实际符号/调用链归纳 RX/poll、连接查找/创建、同步、checksum、TX、释放/回收和控制任务。分组规则与未归类比例可追溯；函数名包含 `sync` 不足以证明节点间连接同步。
- `tcp_send_csum` 采样上升可能来自包长、PPS、软件/硬件校验和配置、采样覆盖或单包成本变化。cache miss 总数不单独证明内存带宽瓶颈；伪共享需要共享地址与一致性流量等证据。
- 空 poll 占比提高只说明采样更常落到该路径。结合绝对样本速率、loops、队列 RX、TX short、refill 和发送端时间线，区分供包、反压、回收和调度候选；不能直接说“iperf 没发”。
- Guest `net_virtio` 不证明 Host 使用某个 vhost/OVS 路径或单线程后端；多 virtqueue 也不证明后端有等量独占 CPU。普通 RX/TX 队列、共享 mempool、控制 ring 是不同资源。
- `rx_nombuf` 增长支持 RX 分配失败；pool 瞬时非零不能排除先前失败。TX 接受量不是物理发送完成量，TX used 不是 TCP ACK；`free` 样本也可来自正常 TX 回收。总 dropped 不等于 TX short。
- 同步通信分别统计业务变化、同步消息、记录数和网络包；消息可批量携带记录，TCP 分段也不等于消息边界。没有真实实现，不假定全互联、每包同步、远端建 conn 或同步回环。推荐观测 `remote_apply_triggered_sync`，同时保留 origin/version 才能验证二次同步候选。
- Host NUMA 基线可提出亲和性/分配核验动作，但不能填补 scaled Host 的 CPU、内存流量、页分布或 backend 调度状态。

## 证据强度和验证

每条 finding 记录 `observation`、计算/推导（如有）、`hypothesis`（如有）、`evidence_refs`、`confidence`、`alternatives`、`missing_evidence` 与 `validation`，映射到实际分析器的等价字段即可。

- 高：该项观测直接、范围和单位明确、来源完整且可复核。高置信度观测不自动成为高置信度因果结论。
- 中：多项同窗证据一致，但有角色/覆盖差异或尚未排除的替代解释。
- 低：单次间接相关、符号缺失、归一前提缺失或主要依赖架构假设。数据不足时允许不给原因排名。

仅有一次运行不能给出重复实验的置信区间。重复数据可报告运行间分布及方法；把相关采样点当独立实验计算显著性会夸大精度。置信度是有理由的证据评级，不是随意指定的概率。

建议应能区分候选：写出控制变量、补采内容、支持/反驳时预期看到的变化和成功指标。优先使用已有日志及低开销每核计数；每条建议明确是补证还是优化实验。未测收益写“待验证”，不生成 SIMD、绑核、增大 ring 或禁用同步的收益百分比，也不自动执行线上变更。
