# Trace 管理与寻优校准

## Trace 的位置与生命周期

「Trace 管理」按原始文件及其副本建立清单。Windows Bridge 新采集的原始 Trace 保存在 Bridge 配置目录旁的 `traces/`，可通过 `trace_root` 配置持久目录；使用者可以复制页面上的路径，在该 Windows 主机的自有工具中打开。持久目录独立于传输 spool，上传完成后的临时文件清理不会清除原始 Trace。本机 HDC 直连沿用本机受管采集目录。

远端 HDC 往返或 Bridge 作业期限不适合当前环境时，可在场景采集页下载“Windows 本地采集包”。解压后执行 `run-capture.ps1`，固定 Runner 直接调用 Windows 本机 `hdc.exe`，默认输出到用户文档目录下的 `HeterogeneousCapture`，也可用 `-OutputRoot` 指定目录。采集完成会生成 `capture_<id>.capture.zip`；在 Trace 管理点击“导入本地采集包”后，平台流式接收并校验 ZIP 成员路径、文件数量、压缩与解压大小、清单、逐文件 SHA-256 及项目身份，再原子发布为标准场景采集 Run。同一内容重复导入保持幂等，同一 Run ID 的不同内容拒绝覆盖。Windows 原目录与平台分析副本互相独立。

平台保留用于分析和建模的副本，各位置分别展示可用、离线、缺失或已删除状态。页面提供搜索、位置过滤、刷新位置、复制路径、下载平台副本和删除指定副本。远端路径不作为浏览器本地链接；Windows 主机离线时不宣称文件已经删除。历史采集通过已有 manifest 索引，不移动历史文件。

删除操作选中一个稳定的副本 ID，并在页面确认。服务端校验项目归属、受管路径、文件身份及当前采集状态；已删除副本保留记录，其他位置与已有分析结果继续保留。删除原始文件会使依赖该副本的再次解析或下载不可用；已导入建模的独立输入副本单独管理。平台不自动清理用户的任意文件目录。

接口：

- `GET /v1/trace-library`：读取原始 Trace 与副本清单。
- `POST /v1/trace-library/refresh`：刷新平台文件状态及在线 Bridge 清单。
- `GET /v1/trace-library/{trace_id}/download?copy_id=...`：下载指定平台副本。
- `DELETE /v1/trace-library/{trace_id}/copies/{copy_id}`：删除指定副本，保留清单元数据。

Windows 持久保留与远程清理需要运行带有 `trace_library` 能力的新 Bridge 脚本。已有 Bridge 不会因平台更新而自动更新其本地程序。

## OPP 与核频搜索

OPP（Operating Performance Point）是硬件支持的一组频率、电压工作点。平台界面使用更直观的「CPU 频点」名称；当前仿真使用归档频率列表，没有电压或能量模型。同一核组共享频率，逻辑 CPU 数不能直接作为物理核心数量。参考定义见 [Linux OPP 文档](https://docs.kernel.org/power/opp.html)。

## 寻优模型与待校准参数

当前模型使用真实 Trace 构造任务依赖图，保留主线程、BuildItem、同步服务调用和等待。每个阶段先按 Trace 账目区分：

```text
T_wall,observed = T_on_cpu,observed + T_off_cpu,observed
T_off_cpu,observed = T_runnable + T_blocked + T_unknown
```

`T_on_cpu` 是实际占用应用 CPU 的时间；`T_runnable` 是已可运行但尚未获得 CPU 的时间；`T_blocked` 包含同步系统服务等等待；无法可靠归类的间隔保留为 `T_unknown`。上述等式只描述 Trace **观测账目恒等式**。预测时，核型和频率模型只换算 On-CPU 工作；非 Binder delay 节点保留实测时长，同步 Binder blocked 不再作为一段固定 delay 重复加入，而由 send → service → reply → wake 依赖链、服务资源与唤醒条件表达。整个回调的预测时间是这些节点和资源共同约束下的 **DAG makespan**。并行分支会重叠，因此 `app_off_cpu_observed_ms` 只用于报告观测账目，不能与预测 On-CPU 直接相加得到预测墙钟。

串行回放通过后，平台在物理核心、共享频率域、worker 数和派发策略组成的搜索空间内执行离散事件仿真。命中实测的任务类型频点性能曲线时，BuildItem 的 On-CPU 成本为：

```text
L(k, class, f) = L_max_opp(k, class) × G(k, class, f)

T_on_cpu,predicted = T_on_cpu,observed
                   × L(target, class, f) / L(reference, class, f_reference)
                   × C(concurrency)

G(k, class, f) = (fmax / f)^e_hi                                      , f >= fknee
               = (fmax / fknee)^e_hi × (fknee / f)^e_lo               , f <  fknee

约束：0 <= e_hi <= e_lo <= 1
```

`L_max_opp` 是该核组、该任务类型在最高 OPP 的实测单位工作量绝对时延；`G` 是同一曲线内借鉴 Roofline 形态的 **CPU 频率—时延分段响应（CPU-frequency analogue）**。二者共同表达核型与频点影响，因此命中曲线后不再叠乘核型倍率。这里的横轴是 CPU 频率；经典 Roofline 的横轴是运算强度，并结合计算与带宽上限，因此两者不是同一个公式。`fknee` 是当前核组、当前负载候选类型的频率拐点；`e_lo` 表示拐点以下较强的频率响应，`e_hi` 表示拐点以上仍然存在的残余响应。计算代理通常会得到接近最高 OPP 的拐点和较大的指数；访存代理通常更早进入平台区，`e_hi` 较小；混合代理位于两者之间。曲线在拐点连续，并以最高 OPP 的实测 CPU 时间归一。

分段响应 `G` **替代**旧线性频率弹性项，不能再把该分段响应乘到 `[(1-e)+e×fmax/f]` 上，否则会把同一份核频收益计算两次。只有阶段未分类、该核组/类型缺少合格实测曲线、训练频率域无效或目标频率在域外，或显式执行旧模型敏感性情景时，才回退：

```text
G_legacy(k, f) = (1 - elasticity[k]) + elasticity[k] × fmax[k] / f
T_on_cpu,predicted = T_on_cpu,observed × core_ratio(k, category)
                   × G_legacy(k, f) × C(concurrency)
```

争用项 `C` 作用于 BuildItem On-CPU 节点，任务执行期间随并发数量变化重新计算；命中双任务实测矩阵时使用有向组合系数，未命中时回退 `1 + alpha × max(0, N-1)`。任务分支另有派发开销。CPU 占用时间、墙钟时间分别记录，core-ms 是资源占用而不是能量。

校准页以三个主参数组组织，并把旧模型参数下沉为兼容回退：

- **任务类型频点性能曲线**：对每个核组的 compute / memory / mixed 三类代理分别覆盖至少 **6 个不同实测 OPP**：其中至少 5 个 OPP 参与拟合，另 1 个中间 OPP完全不进入拟合、只做独立留出验证。每条曲线同时保存最高 OPP 的绝对单位工作量时延和相对分段响应；跨核迁移直接使用目标/参考曲线成本比值。
- **负载组合干扰矩阵**：保持双方核组、物理落核、OPP 与工作集，建立三类代理的匹配单跑锚和九种有向两两共跑组合，用目标任务的 CPU 膨胀拟合 `A←B` slowdown。
- **Worker 任务派发开销**：同一持久 Worker、CPU、OPP 和 payload 下，比较直接批量执行与逐项队列投递/唤醒/完成确认，使用配对差分得到暖态派发 wall 与 CPU 成本。线程创建、绑核、ready 和 join 属于冷启动，不混入每任务常数。
- **兼容回退**：核型 CPU 倍率、线性频率弹性和通用并发 `alpha` 只在频点曲线或干扰矩阵未命中时使用，并记录回退原因；它们不会与已命中的主参数叠乘。

阶段的 compute / memory / mixed 类型来自 PMU 特征规则，状态始终是 **candidate**：例如 IPC 与 LLC MPKI 的证据边界可支持候选分型，但通用 cache miss 不能直接推导 DRAM 字节、实际带宽饱和或经典 Roofline 的运算强度。完整 PMU 事件不足时，已有 IPC、Cache/LLC MPKI 或有效非零基础计数可形成带置信度、已用指标和缺失证据的 Running candidate；只有完全没有有效定量证据时才保留 `unknown`。候选分型只用于选择哪条已实测曲线，不能用一次 PMU 分类直接臆造拐点；通用 Cache 形成的降级候选也不等同于已证实的 LLC/DRAM 瓶颈。分类为 `unknown` 或类型曲线未校准时，平台明确回退旧弹性并在结果记录覆盖比例和原因。

每条发布曲线保存 `frequency_domain_khz`，其值只包含参与拟合的训练 OPP。分段曲线只允许在 `[min(frequency_domain_khz), max(frequency_domain_khz)]` 闭区间内使用；未见的中间 OPP 属于域内插值，独立留出 OPP 专门验证这种迁移。方案把硬件最低和最高 OPP 纳入训练，因此正常使用同一硬件 OPP 目录时覆盖完整频率范围。旧模型缺少合法训练域，或请求频率落在训练域之外时，平台不外推分段曲线，而是显式回退 `G_legacy`，并在 `frequency_model_usage.fallback_reasons` 记录 `missing_calibrated_frequency_domain` 或 `outside_calibrated_frequency_domain`。

有效服务容量、应用线程安全和真实应用并行收益仍需独立证据。未测参数继承原模型并标识来源，不因其他参数拟合通过而视为已校准。完整模型边界见 [BuildItem 条件并行模型](parallel_modeling_model_v1.md)。

## 离线校准工作流

通过质量门禁的画像、方案、拟合和版本还可以封装为随分析 Worker 分发的内容寻址包；格式、哈希、幂等安装、真机样例与应用验证边界见[离线校准模型包](offline_calibration_packages.md)。

「寻优校准」先展示模型结构、参数来源和微基准方法，再提供校准工作台：

1. 选择参考硬件、参数组和重复次数，生成并保存实验方案。
2. 下载微基准源码、执行说明、条件清单及数据模板，在目标设备采集实测数据。
3. 导入包含设备来源、核绑定、实际频率、工作量、CPU 时间、墙钟时间、重复编号以及训练/验证划分的 JSON。
4. 拟合参数并检查样本覆盖、重复稳定性与验证误差；不足的样本保留原因，可补充后再次拟合。
5. 将通过检查的结果保存为模型版本，在「并行建模」选择该版本执行核频寻优。

接口位于 `/v1/parallel-calibration`：`catalog`、`plans`、`plans/{id}/bundle`、`fits` 与 `versions`。当前校准 producer、拟合引擎和新发布模型版本统一为 **1.2.0**；既有 1.1.0 持久记录继续按原内容读取，不原地迁移。模型版本记录实测来源、拟合结果、校准参数与继承参数。微基准校准通过表示模型在代理负载范围内经过检查，应用收益仍以条件预测呈现。

### Desktop 实测 JSON 导入

Desktop 的「实测与拟合」支持选择不超过 **20 MiB** 的 UTF-8 JSON，不再把文件内容塞进 SSH 命令或受限短请求。传输使用 target/profile 绑定的 typed SFTP 合同：

1. Renderer 先解析 JSON，对将要传输的 UTF-8 文本字节计算长度和 SHA-256，并向当前校准方案申请一次性预约；
2. 服务端只在当前 target/profile 的 `state/parallel-calibration-measurement-uploads/` 下返回随机 token 对应的托管路径，预约冻结 `plan_id`、长度和哈希；
3. Main Process 再次核对 SSH 四元组、token、托管路径、长度和 SHA-256，以 `0600`、不可覆盖方式写入 SFTP 暂存文件，并核验远端大小；
4. 拟合入口要求预约和文件同时存在，拒绝符号链接或非普通文件，重新计算服务端 SHA-256，解析有效 UTF-8 JSON 后才调用同一校准引擎；
5. 成功、拟合失败或请求异常都会清理本轮文件和预约；客户端还调用幂等 discard 兜底。创建新预约时会清理超过 24 小时的 token 形态遗留文件。

页面和拟合请求只传 token、方案、大小与哈希，不接受用户指定的任意服务器路径。SSH 目标切换、文件变化、预约不一致、半传输或哈希不一致都不能进入拟合。完整拟合结果仍持久化在服务端；Desktop 响应只返回页面需要的投影。

Desktop Worker 白名单路径为 `POST /v1/parallel-calibration/measurement-staging`、`POST /v1/parallel-calibration/fits/from-staging` 和 `POST /v1/parallel-calibration/measurement-staging/discard`。Web 继续使用 `POST /v1/parallel-calibration/fits`；两条入口最终调用相同的 `CalibrationManager.fit()`，不会形成桌面专用拟合算法。

## 验证记录（2026-10-16）

- Trace 管理及采集、Bridge 相关 111 项测试通过，包含 26 项新增文件管理用例；校准、模型、任务与动作相关 38 项测试通过。两组测试涉及部分共同基础代码，不作为无交集总数相加。
- 微基准通过严格 C11 编译，本机功能运行验证 JSON、CPU 时间及并发证据；未在手机执行校准。
- TypeScript、lint 与生产构建通过。真实 Chrome 在隔离状态目录中验证 Trace 下载、取消删除、删除指定测试副本、方案生成、源码下载、测试样本拟合、模型版本保存及校准模型带入分析，页面脚本错误为 0。测试数据未写入正式校准目录。
- 正式预览只读验证已有 4 条 Trace、5 个可用副本及校准参数页面；原始采集文件保留。Windows 持久存储/清理通过模拟 Bridge 和临时文件验证，未部署实际 Windows Bridge。

## 2026-10-21：阶段特征与组合干扰

独立「负载特征模型」页负责阶段拆解、指标与候选分型，详见 [特征合同](workload_features_model.md)；「并行建模」选择该数据集的 `feature_profile_id`，分析及搜索保存版本与数据指纹。实测串行账目保持独立，特征分类只参与候选成本计算。

校准工作台新增 `pairwise_interference` 参数组，与原核频 / 启动 / 通用 alpha 参数可独立或联合拟合。三种代理内核产生九种有向共跑组合，使用同目标核、频点、工作集下的单跑作为锚点；留出工作量验证后发布双任务矩阵。DAG 每次调度事件重新匹配活动任务组合，并记录匹配和回退的 CPU 时间。详细测量、适用域及可执行采集合同见 [双任务干扰校准](parallel_pairwise_interference_v1.md)。这不是已经完成目标设备实测的声明。
