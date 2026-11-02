# 输入与格式规则

## 身份、角色与覆盖

每个输入文件保留下列信息；沿用已有分析器字段时明确对应关系，不重建一套相互矛盾的标识。

| 项目 | 记录内容 |
| --- | --- |
| 身份 | `artifact_id/file_id`、原始路径/名称、原始字节的 `sha256`、字节数；不能读取原始字节时哈希为空并说明原因 |
| 场景 | `scenario` 与 `role`；典型映射：baseline/single_node_guest、scaled/sixteen_node_guest、host_single_node/host_single_node |
| 范围 | 采集机器/节点、Guest 或 Host、PID/TID/CPU/port/queue、实际观测节点数；标识可脱敏但保持可关联 |
| 时间 | 起止、时区、时长、稳态截取范围、时钟来源、采集顺序/延迟；未标时区的墙钟不能强行跨机对齐 |
| 解析 | `parser` 名称/版本/识别变体、已用段落及行号、单位转换、拒绝段落及原因、`confidence` 与依据 |
| 配置 | CPU/PMU、内核与工具版本、CVS/DPVS build、采样命令及 filters、frequency/period、丢样本/限频/复用信息 |
| 负载 | 节点数、每节点还是集群总吞吐、包长、连接数/CPS、协议、同步拓扑与是否同负载；未知不从文件名补猜 |

`sha256`、`role`、`parser`、`confidence` 对每个文件均有字段位置；无法确认时记录空值/unknown 及原因。角色可以沿用用户明确映射，文件名只能作为候选提示。一个文件包含多个数据类型或场景时按原文片段记录，不复制后再冒充独立采样。

coverage 区分文件数、可解析行/段、可用时间窗、符号解析覆盖与场景覆盖。文件级解析置信度不代表因果结论置信度。记录实际丢弃的行，不将注释和合法标题计为解析错误。单位不明、数值歧义或多角色冲突先局部隔离，继续使用可解释部分。

## perf / HiPerf / 平台变体

- 先识别 report、stat、script 还是二进制采样。`perf.data` 不能按文本解析；有匹配工具时导出，保留导出命令和原文件哈希，否则标记该部分未解析。
- report 按标题识别 `Self`、`Children`、`Overhead`、`Samples`、`Period`、`Command/Comm`、DSO、Symbol。支持空白、制表符与声明过的分隔符；不固定列号，不把 `[k]`/`[.]` 当成函数名。
- 不将 callgraph 缩进子项再次计入平铺符号样本。百分比缺少列名/分母范围时标为 `unknown`；HiPerf 的 Event Count/Percentage 等按该平台导出定义映射，不因名称近似就当成 Linux perf 的同一指标。
- `[unknown]`、地址符号、被截断的名称独立保留，给出未知符号占比。跨 build 的符号变更、内联及 `.isra/.constprop` 可归到候选组，但保留原名、DSO 和匹配方式；不把多个原始条目重复汇总。
- 同一文件可有多个事件块，各自保留 event、范围、总 period/总样本和百分比分母。top-N 或 threshold 截断不是完整总体，不能通过显示行求和重建全量分母；未出现的函数不等于零。
- stat 的 `<not counted>`、`<not supported>`、权限错误为不可用值。区分 sample count、raw event count、已缩放 count、event-specific weight。有复用比例时记录 enabled/running 及是否已缩放，避免二次缩放。
- decimal comma、千分位、`K/M/G`、科学记数法必须结合 locale 和输出格式解释；`1,234` 无上下文时不能擅自选 1.234 或 1234。保留原 token。切勿从百分比及文件大小推算采样时长。

## sar / sadf / CPU 时间序列

- 按每个段落的 header 识别 CPU、网络、内存、调度等列。Linux/sysstat 版本不同会重复标题、改变列集合或缩写；非 Linux 输出不套用 Linux 列定义。
- 识别 12/24 小时、AM/PM、日期切换、`Average:`/本地化平均行和系统重启分界。平均行是该段汇总，不能作为额外时间点与原行再平均；重启后累计计数不能与重启前求普通差值。
- `all` 和每 CPU 分别报告，不一起相加。`%sys` 与 `%system`、`%usr` 与 `%user` 在部分格式中的子项口径不同，按工具版本定义映射。guest/gnice、irq/soft、iowait/steal 可有包含关系，不机械相加。
- 实际间隔变化时按覆盖时长加权；只有汇总行时只能报告汇总，不能生成曲线、峰值或 P95。缺失时段留断点。对不同长度窗口分别记录完整统计和可比稳态子窗。
- 保留接口名及单位；`rxpck/s` 与 `rxkB/s` 不是同一量。网卡统计、应用吞吐和 Guest/Host 流量分开，不能把各层看到的同一报文反复累加为集群吞吐。

## NUMA / topology / affinity

- 分清 `numactl --hardware` 的 node/CPU/distance/free-memory，`numastat` 的分配计数，`numastat -m/-p` 的内存分布，以及 `/proc/PID/numa_maps` 的映射页记录。
- 无参数 numastat 常以 pages 计数；带选项的表可能是 MB。按命令/标题确认单位。页数换算需要 page size；`numa_maps` 可能逐映射记录不同的 `kernelpagesize_kB`，不可统一假设 4 KiB。
- 单次快照只描述累计值或驻留分布。只有同机器、同口径、无重启/复位的前后快照才能求该窗口差值；驻留量的差值不是累计远端访问量。
- `numa_hit/miss/foreign` 是分配目标统计，`local_node/other_node` 也记录分配时 CPU/node 关系；不是 cache hit/miss、远端 load 次数或内存带宽。
- Host 物理 node 与 Guest vNUMA node 编号不能直接对应。需要 vCPU/TID→pCPU、内存策略/实际 backing、NUMA 距离、NIC/backend 绑定才能判断本地性候选。只有 Host 基线快照时，16 节点 Host 状态保持缺失。

## 格式语义的主源

版本以实际采样工具为准；以下用于核对定义，不代表部署版本：

- [Linux perf report](https://github.com/torvalds/linux/blob/v6.6/tools/perf/Documentation/perf-report.txt)：列、过滤、Children、period 和 sample。
- [Linux perf stat](https://github.com/torvalds/linux/blob/v6.6/tools/perf/Documentation/perf-stat.txt)：计数范围、事件与输出选项。
- [sysstat sar 手册源文件](https://github.com/sysstat/sysstat/blob/master/man/sar.in)：时间区间与按列解释。
- [numactl numastat 手册源文件](https://github.com/numactl/numactl/blob/master/numastat.8)：分配计数与单位变化。
