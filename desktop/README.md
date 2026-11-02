# 异构优化工作台 · Windows App 0.1.93

Windows 桌面应用是异构优化平台的正式用户入口，提供 **本地负载采样、Trace 管理、负载特征、硬件模型、寻优校准、并行建模、自定义 Code Agent、本地/远程文件工作区、SSH 终端与服务器环境检查**。Windows 负责 HDC、采样、Trace 原件和真机执行；Linux 收敛为大仓源码访问、代码索引、SDK/工具链编译与 Linux 专用工具 Worker。0.1.93 尚未交付可断线恢复的跨机长任务和构建产物自动传输。应用可安装到电脑，也可直接运行便携版 `.exe`。Windows 包内置 Python 采样运行环境、PMU 采样器与受管校准微基准，日常运行不需要部署完整 Web/API 或安装 Python、Node.js；Agent 软件、HDC 与手机驱动使用电脑上已安装的版本。

当前源码还提供“DPVS 性能分析”中心页面，用于导入单 CVS Guest、16 节点场景 Guest 和单节点 Host 的 perf/FlameGraph、sar/sadf、numastat/numa_maps 文本，生成带绝对值、相对值、可比性和来源的静态报告。该页本地运行，不依赖 SSH；右侧 Code Agent 可通过 `dpvs-cvs-performance-analysis` Skill 深入分析当前结构化证据。数据口径、采集命令和报告边界见[DPVS / CVS 线性扩展性能分析台](../docs/dpvs_cvs_performance_analysis.md)。

0.1.93 将本机私有设备的 Trace 预检补到 HiTrace 服务状态层。即使 TraceFS 的 `tracing_on` 已经是 0，服务仍可能保留上一次 `RECORDING_LONG_BEGIN`；Runner 1.2.15 在独占会话内有界调用 `hitrace --trace_finish_nodump` 并复核所有 TraceFS 根，避免新采样以 `OpenRecording errorCode(1103)` 失败。该动作不进入 Bridge/共享设备路径。Trace finisher 的自然冲刷窗口按缓冲区大小从 3 秒分级增加，计划允许的 1 GiB 缓冲区为 18 秒，仍受 25 秒硬上界约束。PMU 二进制身份已确认且兼容路径已接受、但在启动前被 Trace 或操作失败中断时，结果显示 `PMU_NOT_STARTED`，不会再引导用户重复部署采样器。

0.1.92 将 Code Agent 的“输入后提交延迟”统一为 `100–5000 ms`，新配置仍默认 1000 ms：短消息显式设为 100 ms 时可在约 100 ms 后实际发送 Enter，长文本按字节数增加有界等待，观察到终端输出时只再等待短静默窗口。“失败自动重试”放到连接配置的第二个页签，负载采样执行区同时显示开关与手机授权状态，并可直达该配置。Runner 1.2.14 对重启后的精确旧会话补齐采样器与 Trace 收尾，采集脚本中的 Trace heartbeat、PMU、HiLog 和 `hitrace --trace_finish` 均采用有界退出；默认 120 秒步骤的宿主等待从 300 秒收口为 165 秒上限，失败时额外拉取最后清理阶段和强制终止事件，成功路径不增加 HDC 往返。

0.1.91 在并行建模页新增“一键导出 HTML 报告”。导出从当前已保存搜索重新读取冻结的数据集、分析、特征、硬件和校准血缘，嵌入校准方案、拟合质量、优化公式、核心组合及最优配置，并附带按历史方案由当前部署资产重建的校准代码快照；缺失证据和 Desktop 有界投影均明确标记。报告无脚本、无外链，Desktop 使用原生保存窗口和原子写入。优化模型公式卡改为目标函数、约束、字典序、两级求解和符号表的全宽布局。

0.1.90 新增“自动化与授权”配置。用户可分别开启“出错自动发送助手解决并重试”“授权全部手机操作”和“授权全部服务器操作”，三项均默认关闭。接入恢复合同的步骤失败时，工作台冻结操作键、所选设备、计划 revision 或当前 SSH 目标，将脱敏上下文提交给 Code Agent；Agent 完成修复和最小验证并调用类型化完成工具后，平台仅重试原步骤一次。授权只对自动恢复经工作台工具发起的动作生效，切换设备或 SSH 目标不会把旧任务授权带到新目标，用户主动操作继续沿用原合同。

0.1.89 收敛“负载与模板”的首屏信息密度。负载模板和计划名称直接显示；目标应用包名、Ability、步骤列表、界面准备、执行范围及高级模板参数移入默认关闭的“模板详情”。执行主按钮不随折叠隐藏；字段校验失败或 Agent 返回 blocker 时，页面自动展开详情并把焦点移到对应字段；模板和历史计划切换不会擅自改变用户的折叠选择。

0.1.88 统一了错误到助手的诊断入口。所有桌面错误通知均可一键提交，自动附带页面、操作位置、错误码、设备与脱敏上下文；后台采样最终失败同样产生通知。明确选中的本机直连设备按私有设备策略执行 Trace 预检：先严格核验平台 claim，必要时用 `hitrace --trace_finish` 保存旧 Trace 并确认 `tracing_on=0`，不按进程名批量终止系统服务。Bridge/共享设备继续保护归属不明的 Trace。崩溃恢复只向新任务传递本次启动确认中断且 run、目录、项目、计划和设备完全一致的所有权证据。

0.1.87 将显式初始化收敛为一次长期规则写入，环境、Skill、工具合同和运行状态留到真实任务中按需验证。Agent Workspace lane 在连接后后台预热；执行记录采用异步 single-flight 最新快照，避免 AppData 慢盘、Defender 或索引器把 Electron 主线程与本地 Broker 一起阻塞。经验演进页分别展示 Agent 观察跨度、平台墙钟、远端执行、排队、通道获取、RPC 往返与通道启动基线，明确这些口径存在包含关系、不可相加；页面以最大单次耗时而非同类累计耗时标识最慢平台操作。

0.1.86 新增渐进式工具发现、Code Agent JSONL Session 可视化和采样归属恢复。Agent 先看到全量工具的短摘要，只对实际选中的工具读取完整合同；经验演进页可自动发现 `%USERPROFILE%\.cac\projects`，展示消息血缘、工具调用/结果、观察耗时、错误及平台 Skill/工具使用情况。平台自己的遗留 Trace 通过持久 claim、boot ID、token 和持续 marker 恢复；来源不明的 Trace 不会被强制停止。计划包、采样文件和归档改为唯一临时名、排他发布与哈希幂等复用，重复回传不再覆盖同名文件。

0.1.85 收敛负载采样计划生成链路。标准模板已经完整且没有额外要求时，页面本地确认后直接聚焦执行按钮；需要 Agent 补全时，只投影有界的可编辑步骤、引用到的 selector、允许动作和 blocker。`capture_update_plan` 以 plan revision 与 request ID 做原子 CAS 和 exactly-once 围栏，只合并差量并返回紧凑结果，保留采集器、PMU 和执行策略；该任务在 Broker 中只允许一次参数完全匹配的更新调用。成功、失败、Agent 退出或 120 秒截止都会结束页面等待；切换计划或继续编辑后，迟到结果只进入“载入更新”。CLI 回退可直接调用该工具，不再扫描全量工具 Schema 和 catalog。

0.1.84 在 Code Agent 配置页新增“原生 MCP 配置”卡片，提示词可展开审阅并一键复制。它引导当前 Agent 识别官方 MCP Loader，核验 `mcpServers.desktop-ssh`，按外部配置、注册命令、原生配置合并或启动参数的优先级接入，并要求 MCP 子进程继承动态 Broker 环境。配置后的验证只接受原生 `remote_status` 与 `capture_catalog`，重启前不会把“已写入”误报成“已生效”。该任务由用户显式复制，不自动发送，也不进入长期记忆。

0.1.83 移除活动栏顶部的 4px 内边距，让活动栏背景、导航列表和首个导航项从标题栏下边界直接开始。Electron 布局回归分别锁定标题栏底边、活动栏顶边、导航列表顶边和首项顶边，防止后续主题或布局调整重新引入缝隙。

0.1.82 在 Code Agent 标题栏增加“初始化 Code Agent”按钮，位置紧邻设置按钮。它是用户显式触发的真实 PTY 任务，不在启动时自动运行：初始化任务检查 MCP、`HETERO_DESKTOP_TOOLS` CLI 回退与 `HETERO_AGENT_SKILL_ROOT`，读取托管的 `heterogeneous-workbench-tools` Skill，用只读 `remote_status` 和 `capture_catalog` 验证入口，并要求 Agent 只把稳定规则写入长期记忆。工具 Skill 解释了 `capture_save_plan`、`remote_status`、`workspace_*` 等能力的位置和调用合同；CLI 的 `--list-tools` 可直接返回当前版本完整工具 Schema。长 bracketed paste 会按提示词大小及本次 TUI 输出等待编辑器收敛，随后只发送一次配置的 CR/LF；提交结果保留等待原因和实际时长诊断。

0.1.81 停止向 Code Agent 对话自动写入启动上下文：无论新配置还是旧配置中的 `bootstrapContext=true`，启动和重启均保持 PTY 零写入，首个用户任务原样发送。Code Agent 配置页新增“Agent 长期记忆”，可预览并复制一条稳定的系统提示；用户将其交给自己的 Agent 保存一次，平台不声称代替第三方 Agent 完成持久化。0.1.80 的 164px 活动栏与 20px 图文间距继续保留。

0.1.79 的活动栏保持固定 50px，不改变内容区几何尺寸；悬停任意区域时以覆盖层展开全部功能名称，文字区域可直接点击，选择后收起并在重新进入活动栏时恢复。连接配置的“界面”页可关闭该行为，回退到单项提示，选择会保存在当前电脑。

0.1.78 新增四项工作台合同：并行建模页按当前目标展示最低时延或预算内最低频点的字典序公式、可行域和稳健约束；历史搜索从冻结的 `search_configuration` 恢复目标、预算、核组、Worker、OPP 子集和稳健性，不从 winner 反推。Code Agent 启动时使用每会话 readiness token 预加载精简的本地/SSH/工具上下文；所有等待有时限，失败后自动清理、通知和降级，`Ctrl+C` / `Ctrl+D` 可绕过队列，登录式或菜单式 CLI 可关闭自动上下文。资源管理器新增本地/远程双来源和树上方 Search/Replace，共用 Monaco 与多标签；本地目录使用 Main Process grant，远程搜索使用独立持久 lane，批量替换以 revision/CAS 防止覆盖搜索后的并发修改。

0.1.77 完成负载阶段与硬件校准证据的贯通：逐阶段保存真实 Trace 的逻辑 CPU 驻留，并只通过数据集 `hardware_profile_id` 对应画像中 `core_groups.logical_cpu_ids` 映射核组，展示主驻留 CPU/核组和迁移或混合驻留；缺少画像时保留 CPU 编号并标记核型未知。分型判据与微基准设计改为表格下方默认折叠说明。受管微基准用目标线程用户态正式计时窗内的 `perf_event_open` 采集 cycles、retired instructions、generic cache references/misses，保留 enabled/running 缩放证据、IPC 与 Cache MPKI；事件不可用不阻断有效时延样本。页面公开频响分段公式；统计质量或覆盖门失败时仍可发布 `risk_calibrated`，但结构/来源完整性继续阻断，风险等级、证据等级与失败门禁随版本进入寻优。点击活动栏后 hover tip 立即隐藏。采样数据集按用户选中的设备稳定身份关联硬件画像；没有唯一且可核验关联时保持未绑定，不按序号、CPU 编号或列表首项猜测。

0.1.76 为频点曲线增加源码静态强度、同窗 PMU 强度状态和代理源码吞吐，并提供显式可选的五档 Classic Roofline 附加实验，以已知 FLOP/最小源码 Byte 扫描实测带宽侧、计算侧与拐点。该历史能力继续保留。

应用流程、机会推理、代码仓、代码知识库、优化知识库、技能列表与经验演进已纳入 Desktop 工作流。共享 Web React 组件当前通过受限的持久 Platform RPC 读写服务器状态：页面请求与 Code Agent 的应用流程工具在同一个已认证 SSH client 上使用独立的长驻 exec channel，各自复用 Python Worker，不再为每次调用重启解释器，也不会让 Agent 工具调用占住页面请求通道。页面任务直接提交到右侧本地 Code Agent；本机同时记录有界的真实交互摘要、PTY 可见输出片段和经 Desktop broker 发生的工具等待。该版本建立了 local-first 的能力边界，但迁移页面的多数领域状态仍位于远端 `HETERO_STATE_ROOT`。

0.1.26 的 SSH、内部 Code Agent 与 HDC 发现已由用户在 Windows 验证成功，0.1.27 接入本地采集闭环。0.1.41 增加连接级“服务器环境”配置、只读预检、托管目录初始化和 Agent 补齐入口，并把 Web 的“负载特征、寻优校准、并行建模”三个真实 React 页面迁入 Desktop；页面共用同一业务组件和服务端模型，不维护桌面专用算法。架构与验收边界见[设计文档](../docs/desktop_workbench_design.md)。

## 运行 Windows 便携版

0.1.33 为源码启动补齐独立的 Python 离线包与企业镜像入口，并让 Python 准备步骤继承 npm 的有效代理和企业 CA。npm、Electron 与 Python 是三条独立准备链路；现有 0.1.29 安装版和便携版已经内置 Electron 与 Python，不受源码首次下载问题影响。

0.1.34 新增源码运行的本机 Python 入口。显式设置 `HETERO_DESKTOP_PYTHON` 后，校验通过的 CPython 3.11–3.15 可直接承载桌面采集，跳过内置 Python 下载；安装版、便携版和 `-Package` 仍固定使用随包提供的 CPython 3.13.17。

0.1.35 统一了 Web 与桌面的顶栏、侧栏、页面标题、卡片、状态色和终端剪贴板行为。0.1.36 增加受 SSH 工作目录约束的远程文件树与编辑器，并进一步对齐场景采集页。0.1.37 采用 VS Code 风格深色工作台：Code Agent 在右侧全高贯穿，SSH Terminal 位于内容区底部且可折叠，活动栏只显示图标并在悬停或聚焦时展示名称；当时资源管理器可独立展开或收起，0.1.72 起改为固定的独立服务器工作区。SSH 密码由操作系统安全存储加密，按服务器账户留空复用。0.1.38 增加全局缩放快捷键和重启持久化。0.1.39 修复慢速 SFTP 服务端上的文件关闭竞态，并扩大 Code Agent 默认宽度与拖拽上限。0.1.40 增加深色、浅色和“白 + 黑”主题；混合主题保持右侧 Code Agent 深色，中央页面与 SSH Terminal 使用浅色。0.1.41 确立 Desktop-first 拓扑，并把 Linux 所需外部资源与平台托管目录拆分配置。0.1.42 合并页面与系统菜单、简化 Agent/SSH 交互，并增加多 SSH 终端标签和只读 Agent 执行视图。0.1.43 自动初始化首次分析所需的服务器 Profile，并让本机采集与 Trace 管理可将完整 `capture.zip` 直接转为负载数据集和特征版本。0.1.44 让 Linux 分析 Worker 正式兼容 Python 3.10，修复负载特征页的 `datetime.UTC` 导入失败，并同步兼容服务器 Profile 的运行库哈希校验。0.1.45 将五个工作台菜单改为随页面缩放的渲染器标题栏，移除产品名与版本文字，继续使用 Windows 原生窗口控制按钮。0.1.46 自动发现远端分析 Worker 与既有虚拟环境，重建过期服务器 Profile 并重试；安全自动恢复仍失败时，可将脱敏上下文一键提交给 Code Agent。0.1.47 将应用图标移到“编辑”左侧，修正原生下拉菜单在缩放、多屏和窗口边缘下的锚点坐标，并用光学居中的 CSS chevron 统一文件树目录展开符。0.1.48 增加远端目录树的有界后台预取与 10 分钟非重入刷新，并将带 SHA-256 manifest 的最小分析 Worker 随 Desktop 发布；远端缺失时自动部署到当前 profile 的托管 assets 后重试分析。0.1.49 把 FastAPI 导入限制到 Web 路由创建阶段，托管 Worker 的三个分析目录可在仅有 Python 3.10+ 标准库的服务器环境中运行。

0.1.50 为负载特征到并行建模的每次带入增加独立 revision，重复带入同一数据集和特征版本也会重新生效；并行建模以统一“负载归档”直接展示采集、文件导入和历史归档负载，并对已经物化的内置样例去重。

0.1.51 新增“硬件模型”页和本地设备行中的“提取硬件模型”动作。提取只执行固定的只读 HDC 查询，读取 CPU policy、核组、逻辑 CPU 与 OPP，设备序列号仅以 SHA-256 进入证据；新配置会得到明确标为“拓扑迁移、待校准”的兼容代价模型。并行建模可将原负载派生为绑定该配置的新版本，保留旧负载和既有分析。场景采集的步骤、准备、设备和 PMU 区域同步改为紧凑等高布局。

0.1.52 以逐步完成任务为主线简化 Web 与 Desktop：活动栏入口及页面标题统一为“负载采样”，标题只保留任务名称，解释通过悬停或键盘聚焦查看；“连接配置”齿轮固定在活动栏底部并打开既有配置抽屉。计划生成、设备采样、结果导入、特征提取、硬件保存、校准和并行搜索等步骤各自只突出一个整宽主操作，长任务不跳页，在原步骤中展示状态、进度和取消入口。任务完成后页面滚动并聚焦下一项可用主操作，用户可直接按 Enter 继续。平台只在唯一候选时自动选择设备或硬件配置，不以目录第一项代替用户决策。用户界面采用“负载采样”“远程采集端”等业务词汇，内部 Bridge、API 路径和 page ID 继续作为兼容实现细节。

0.1.53 让硬件提取兼容缺少 `tr` 的精简设备 shell，并把 CPU 画像、编辑表单、拓扑迁移模型和并行搜索改为动态核组，不再限制恰好三个频率域或八个物理核。标准 cpufreq policy 不存在时回退到 `cpuN/cpufreq`；零域错误会列出策略候选与缺失证据。该版本曾以 package/core ID 将 VYG-AL00 概括为 12 核、4/6/2 三个频率域；0.1.75 起分别提取物理核和逻辑 CPU，避免把 SMT 逻辑线程计为额外物理核。负载采样把设备连接放到第一步，采集模式和 PMU 主参数改为紧凑横向布局，历史计划只保留下拉框并可直接执行；修改计划后保存为新版本。

0.1.54 将负载特征页改为统一数据集入口：单个选择器分组列出本机采样步骤、内置历史归档和既有归档数据集，本机步骤按“`[计划名称] Step N: 步骤名称`”命名；唯一主操作为“提取负载特征”。逐阶段表格后展示 BuildItem 时间位置图，点击时间条即可在图下查看对应调度与 PMU 详情。活动栏当前页改用蓝色图标、加粗蓝色指示条和页面底色，浅色、深色与混合主题保持一致的选中语义。

0.1.55 将 Web 与 Desktop 的计划动作统一为一行：从左到右为固有宽度的“保存计划”、占约三分之一可用宽度的已保存计划选择器，以及填满剩余空间的“在设备执行采样”。不再显示单独的“已保存计划”标题；执行中由同一按钮原位切换到“取消采集”或取消中状态，“保存计划”旁不再生成独立取消按钮。

0.1.56 统一主操作和硬件模型页面的尺寸体系：“让助手生成计划”采用与“在设备执行采样”一致的主按钮表现，三种工作台主题中的 hover 状态均保留清晰文字；硬件模型提取行的设备选择、读取设备和提取配置均为 38 px 高，核组卡片与已登记模型卡片分别按所在行等高排列。

0.1.57 为 Desktop 服务器分析请求增加有界排队：共享页面最多并行使用两条 SSH exec 通道，隐藏页面刷新不再撞上 8 命令硬上限；并行搜索仍是一条远程命令，专用执行窗口为 10 分钟。该命令内部以最多 8 个受控 Python 进程批量仿真配置，排序、敏感性分析与持久化仍在主进程完成。最短时延目标默认使用最高 OPP，频点预算目标自动切换为全 OPP。选择另一硬件后，点击“拆解账目与回放基线”会先自动建立不可变负载版本，再选择兼容模型并继续分析；手动“生成新硬件版本”动作已经移除。连接配置抽屉从标题栏下方开始，在工作台缩放后也不会被窗口控制区遮挡。

0.1.58 让负载采样页在初始化和每次重新进入时自动执行本机 HDC 设备发现；同一 HDC 路径的并发刷新复用一个请求，路径变化时新请求接管结果，短暂缺失不会清除用户明确选择的手机。页面优先载入最新已有计划，保存计划及 Agent 回填计划后自动切换到新版本。采集模式与 PMU 参数在正常宽度下同行展示，“保存计划”和历史计划选择器等宽，执行按钮保留两倍列宽。

0.1.59 将计划动作列宽调整为 `0.5:1.5:2`，让保存按钮收窄并扩大历史计划选择器；并行建模的已保存搜索隐藏内部 ID，以目标、时间和预测时延辨识。启动时分别恢复已配置的 SSH 与 Code Agent，任一配置缺失时自动打开对应配置页，另一条完整链路仍可启动。本轮同时迁入应用流程、机会推理、代码仓、代码知识库、优化知识库、技能列表和经验演进，新增页面保持右侧 Code Agent 与下方终端常驻。

0.1.60 支持右键 SSH Terminal 标签进行横向拆分，两个真实 Shell 同时交互，分隔线可拖动或用键盘调整。资源管理器支持多文件标签，每个标签独立保存缓冲区、revision、未保存状态、光标和滚动位置。优化知识库增加 NPU API、GPU API 和自建 API：用户输入服务器源码目录与异构单元名称后，平台登记 Source Connector，并复用 `custom-accelerator-api-kb-builder` Skill 的合同扫描与质量分层。代码知识库若返回 Tree-sitter C/C++ 解析器缺失，Desktop 会为当前 target/profile 准备隔离的托管解析环境并重试。

0.1.61 恢复完整的机会推理方法：平台自动读取本机所选 HDC 设备的厂商、型号、系统版本、芯片与 CPU 画像；从全部真实 Trace 工件中提取热点；按 Trace 声明位置或有界符号检索匹配源码；再复用 Web 的硬件中立代码画像与能力卡准备模型路由上下文。Raw HTrace 热点直接复用 Web 的父子调用栈扣减、HiTrace Tag 解码和模块分组，页面明确把结果标为插桩 Span 自耗时估计；转换数据不可用时降级为 Trace Span 指标，不把它称为 CPU self-time。平台不按关键词给单元或 API 打分，右侧 Code Agent 先在隔离上下文中完成 NPU/GPU/DSP/媒体路由，再沿主目录核验 API，并独立评估 CPU 并行、SIMD 和算法方向。应用流程与机会推理的真实 Trace 选择器都合并服务器已物化记录和本机 Trace 管理记录，选择本机步骤后才上传、解析并绑定当前代码仓。标题栏菜单支持短暂悬停后自动打开；资源管理器改为复用交互和预取两条长驻 SFTP 通道，故障通道自动淘汰，后续操作重新建立。

0.1.62 打通 Desktop 微基准实测 JSON 的受管 SFTP 导入、服务端拟合与模型发布，单文件上限 20 MiB；单任务成本模型分别展示 Trace 观测 On-CPU / Off-CPU 账目，只缩放 On-CPU，并用非 Binder delay 节点和同步 Binder 因果链构造 DAG makespan。按核组及 compute/memory/mixed 类型拟合连续分段核频曲线，并把 Trace+PMU 分型贯通到寻优。

0.1.63 将服务器分析请求改为持久 Platform RPC：连续页面操作复用 `page` channel，Code Agent 的应用流程工具复用独立的 `agent` channel；两者共享已认证 SSH client，但具有各自的 Python Worker、请求队列、取消和故障边界。Desktop 本地只向 Agent 暴露已经完整迁移的 `application-execution-flow-reconstruction` Skill，并在每次物化时校验精确文件集合、大小和 SHA-256。`agent-runs/` 保存任务摘要、首个可见输出时延、有界 PTY 输出片段、Platform RPC 与 Agent `remote_exec` 等真实操作证据；Bearer、密码、token、API key、私钥块和工具错误中的同类字段在写盘前脱敏，PTY 静默只标记为未归因等待。设计同时明确 Global Host、Workspace、Window Instance 三层状态以及 local-first/Remote Worker 边界；当前产品仍是单窗口，且多数迁移页面的领域状态仍在远端 `HETERO_STATE_ROOT`。

0.1.64 优化分析页面的首次读取与排队语义。负载特征页读取负载归档、特征版本与分析配置；硬件模型页读取硬件画像与兼容代价模型目录；寻优校准页读取校准方法、方案与历史拟合；并行建模页读取负载、分析与寻优历史。这些操作不加载大模型权重，也不发起模型推理。持久 stdio Worker 按状态根复用 `ParallelModelingManager`，避免多个页面请求反复执行发布版建模 bundle 的完整性校验。页面通道按同步 Worker 的实际处理能力保持单并发，当前活动页的待执行请求优先于已隐藏页面，同优先级保持 FIFO；加载区同时改为具体的数据名称。该版本没有合并 bootstrap 请求，也没有增加历史摘要索引，大型历史目录的完整读取成本仍然存在。

0.1.65 把“实测与校准”改为默认的一键真机流程。Desktop 自动提取当前设备的核组、物理核代表 CPU 和 OPP，核验控频权限后部署带 SHA-256 清单的 AArch64 OpenHarmony 微基准；平台按冻结计划完成锁频、绑核、计算/大工作集带宽屋顶、任务类型核频响应、核组倍率、并发争用和空任务投递测量，再自动拟合并做独立留出验证。频率不可控、设备画像变化、落核或实际频率未核验时明确阻断，不生成伪测量；取消与退出会等待清理并恢复原始频率边界。经典 FLOP/Byte Roofline 已可实测，Instruction/Byte Roofline 仍等待 retired instructions 与 DRAM bytes 的同窗 PMU 对齐。JSON 导入只保留在高级兼容入口。

0.1.66 把校准主流程改为“指定硬件模型 → 一键生成微基准并校准”。页面列出硬件模型库的全部有效模型并显示来源；用户可跳转到硬件模型页，从当前设备提取配置或手动添加。多模型时平台不替用户选择；单模型可直接进入默认路径。模型库发生新增、归档或恢复后校准页会立即刷新。执行时直接使用所选模型建立计划，不再隐式创建画像；当前真机的核组、物理核和 OPP 仍会在部署前重新提取并严格比对。

0.1.67 为经典 Roofline 增加负载计算强度滑块。频率滑块控制 `Ppeak`，计算强度滑块按对数尺度控制 `I`，图上的“负载”点实时移动并以 `min(Ppeak, BW×I)` 取得纵坐标。图内移除拐点文字和固定负载长标签；参考线与动态屋顶接近时自动让位，负载标签靠近边缘时改变方向，避免交叠。

0.1.68 将经典 Roofline 的频率下限扩展到 20% OPP，并同步显示 20% 计算上限参考线。`Ppeak/BW` 交点向横轴投影为动态分界线，带宽斜线下方的左侧区域标为 `Memory-bound`，水平计算屋顶下方的右侧区域标为 `Compute-bound`；区域、分界与标签随频率一起移动。

0.1.69 修复 Windows Git 换行转换造成的受管校准微基准回执哈希失败。新检出通过 `.gitattributes` 保持完整预构建目录的原始字节；旧工作区只在 CRLF 转回 LF 后精确恢复既有清单哈希时兼容，打包时重新写成规范 LF。ELF 不做任何换行转换，二进制大小、哈希和 AArch64 ELF64 身份独立校验。操作错误统一显示为窗口顶部悬浮通知，支持点击正文或复制图标复制完整错误、点击关闭，且不会阻塞页面或让旧通知中断新的重试。

0.1.70 将 SSH 与 Code Agent 收敛为配置驱动的连接生命周期。SSH 和 Agent 面板不再显示重连、启动或停止按钮；保存 SSH 配置会在目标或认证信息改变时自动切换连接，离线时点击“+”新建终端会先按保存配置连接，再创建并复用主终端。保存 Code Agent 配置会在未运行时自动启动，在可执行文件、参数、工作目录或环境变化时重启；唯一的手动“启动 / 重新启动 Agent”入口位于“连接配置 → Code Agent”的“保存配置”右侧，用于显式重启或在进程退出后重试。

0.1.71 增加统一的 Remote Workspace Transport。资源管理器、后台目录预取、Code Agent 的文件读取/检索/写入与远端命令都复用已认证 SSH 连接上的长驻 framed RPC；远端 Python 标准库 Worker 通过 SSH channel 的 stdin 启动，不监听 TCP 端口，也不要求服务器部署常驻服务。`interactive`、`prefetch`、`explorer-search`、`agent`、`platform` 五条 lane 共享协议、目标围栏和观测口径，但具有独立队列与故障边界。Worker 无法启动或握手时，目录与读取可使用既有 SFTP，尚未发送的短命令可使用 direct exec；保存只允许走具备跨进程版本锁和原子提交的 Worker，不退回存在并发覆盖窗口的 SFTP。已经发出的写入或命令不会因响应不确定而被盲目重放。批量工件传输继续使用 SFTP，交互 Terminal 继续使用独立 PTY channel。

0.1.72 将资源管理器收敛为独立服务器工作区。左侧只显示以 SSH 工作目录为根的远端文件树，右侧使用多标签 Monaco 0.53 编辑器，中间分隔条支持拖动和键盘调整；重复点击已激活的资源管理器图标保持当前布局，不再折叠文件树。Monaco 与通用编辑器 Worker 随应用离线打包并受本地 CSP 约束，按文件扩展名提供语言高亮、查找、多光标、每标签撤销/视图状态、三态主题与 `Ctrl+S` 保存。文件读写继续沿用 0.1.71 的 revision/CAS、BOM/换行保留和 SSH 目标切换保护。连接测试客户端移到“连接配置 → 工具集成”，资源管理器页面不再混入连接状态、演示和远程执行记录；Markdown/Mermaid 预览保留为复用既有安全渲染组件的后续扩展点。

0.1.73 将寻优校准主线改为任务类型频点性能曲线、负载组合干扰矩阵和 Worker 任务派发开销。频点曲线保存“核组 × 任务类型 × OPP”的实测绝对单位时延和分段插值，命中时不再叠乘核型倍率；核型倍率、线性频率弹性和通用争用 alpha 仅作兼容回退。Worker 使用相同目标核、OPP 和 payload 对比 direct-batch 与逐项 queue-dispatch，分别保存 wall、Worker CPU、producer CPU 及逐任务差分；线程池冷启动不混入暖态派发常数。受管微基准已升级并重新发布 AArch64 v4 产物。

0.1.74 增加证据分级的 Running 负载候选：完整 PMU 不足时，使用已有 IPC、Cache/LLC MPKI 或有效非零基础计数生成带置信度和缺失证据的 compute/memory/mixed candidate，service/scheduler 继续保留为阶段主特征。通过质量门禁的真机代理微基准可作为离线包随分析 Worker 安装；寻优校准页底部按实际核组 × 任务类型展示实测 OPP 中位数、拟合线和适用域。服务器工作区增加 Markdown/Mermaid 安全预览，以及重命名、复制路径、文件复制/粘贴、分块下载和预览。

0.1.75 修正自动校准完成后的清理、CPU 拓扑与实际频率证据。实测计数完成后，设备频率恢复仍属于同一次运行：执行器在恢复完成前保持活动状态，界面持续轮询并显示“正在恢复频率”，只有原始频率边界恢复成功后才发布终态并进入拟合；设备在线时平台按有界间隔自动重试，应用重启或设备重连后也会继续恢复。远端临时微基准删除失败只作为清理告警，不再冒充频率恢复失败。硬件画像分别保存和展示物理核、逻辑 CPU 及两者映射；`core_cpus_list` / `thread_siblings_list` 必须完整互证，两者都缺失时 package/core ID 仅作估算，不同 ID 不再被当作独立物理核证据。受管微基准 v6 在目标窗口前后读取 `/proc/stat`，逐个核验目标物理核其余逻辑线程；同时把 target/helper 频率绑定到各自逻辑 CPU 的 `cpuinfo_cur_freq`，最多 12 轮同负载预热并要求末尾连续 3 次命中目标 OPP ±3%。policy fallback 仅供诊断。频率或同胞线程质量门失败时，Desktop 对同一个 repeat 最多补测两次，并保留原始尝试、来源路径、频率范围、重试次数和失败原因；v4/v5 样本不能进入生产拟合。

1. 发布流程或 Windows 本机构建 0.1.93 后，可运行 `Heterogeneous-Workbench-0.1.93-win-x64-setup.exe` 安装，也可双击免安装的 `Heterogeneous-Workbench-0.1.93-win-x64.exe`。仓库不提交 `desktop/release/` 二进制；日常源码更新优先使用下文的 `run-desktop.ps1 -Update`。两种运行方式共用用户数据目录，旧版连接配置自动保留。
2. 在“连接配置 → SSH 服务器”填写地址、端口、用户名和远程工作目录（例如 `/srv/workspace/project`），选择密码、私钥或系统 SSH Agent，然后保存配置。已连接时更改目标或认证信息，保存会自动切换到新连接；尚未连接时由下一步“+”按保存配置建立连接。
3. 下方终端可直接输入 `pwd`、`git status` 等命令。SSH 离线时点击标签栏的“+”，工作台会先按已保存配置连接，再创建并选中主终端；在线时可在同一认证连接上建立最多 8 个独立交互 Shell。每个 Shell 有独立 `terminalId`、尺寸和输入流。右键终端标签选择“拆分终端”可同时显示两个 Shell，拖动中间分隔线调整宽度，再次右键可取消拆分。选中文本后按 `Ctrl+C` 复制，无选区时 `Ctrl+C` 仍会中断当前命令，`Ctrl+V` 粘贴。
4. 在“连接配置 → Code Agent”设置本机可执行文件、参数数组和工作目录并保存。未运行的 Agent 自动启动；影响进程的配置发生变化时，平台按新配置重启。也可导入现有 Web 平台 Agent manifest，导入后将 Linux 可执行路径改为 Windows 路径。“Agent 长期记忆”提供一次性复制提示；把它交给 Agent 保存后，工作台启动时无需再向对话注入准备消息。需要手动重试时，使用“保存配置”右侧唯一的“启动 Agent”入口。
5. 如需无人值守恢复，在“连接配置 → 失败自动重试”先开启“出错自动发送助手解决并重试”，再只开启该工作实际需要的手机或服务器操作授权。负载采样执行区会显示当前状态和直达入口。关闭自动恢复总开关或本任务所需授权，会阻止尚未重试的自动任务；普通错误仍可在通知中手动发送给助手。
6. 在右侧 Code Agent 的真实 CLI 终端中直接交互，或使用业务页面的 Agent 动作提交带上下文的任务。桌面不再叠加第二个 Agent 输入框。Agent 的工作区文件工具与 `remote_exec` 复用已经认证的 SSH 连接和独立 `agent` lane，取得 revision、检索结果、退出码与结构化输出；固定只读的“Agent 执行”标签同步展示命令与输出，但不会把命令写进用户正在操作的 Shell。

工作台启动时会分别检查已保存的 SSH 与 Code Agent 配置：配置完整的连接自动建立，缺少必要字段的连接直接打开对应配置页。密码认证只有在系统安全存储中已有该目标的密码时才会自动连接；私钥认证需要已保存私钥路径。Renderer 重载时会复用仍在线或正在建立的 SSH 与 Agent，不重复启动进程或替换终端。

首次 SSH 连接自动记录服务器指纹，后续核对同一地址与端口。SSH 密码使用 Electron `safeStorage` 绑定当前操作系统账户加密，密文独立保存在用户数据目录的 `ssh-credentials.bin`；页面只显示“已保存”状态，不回显密码，`settings.json` 也不包含凭据。相同服务器、端口和用户名再次连接时，密码框留空即可复用；输入新密码会更新安全凭据，设置页的“忘记当前密码”只删除当前目标的密码。系统安全存储不可用时操作会失败；密文损坏或无法解密时，显式输入新密码会先把原文件隔离为不覆盖的 `.corrupt-*` 备份，再建立新的安全凭据。私钥口令仍仅用于当前会话。服务器更换主机密钥时，需要核对后更新用户数据目录中的 `known-hosts.json`。桌面不会修改系统 SSH 配置。

“连接配置 → 工具集成”中的“运行连接演示”会启动**连接测试客户端（不是 AI Agent）**，实际调用远程工具执行 `pwd && uname -a && ls -la`，用于先确认端到端链路。恢复自己的 CLI 时，在 Code Agent 配置页保存对应配置；平台会按新配置启动或重启进程。

### 远程 / 本地工作区浏览与编辑

SSH 连接成功后，活动栏中的“资源管理器”直接打开独立服务器工作区，并列出配置中“远程工作目录”的根内容。页面固定为“远端文件树—可调分隔条—多标签 Monaco 编辑器”，不再展示连接概览、连接演示或远程执行记录；连接演示位于“连接配置 → 工具集成”，执行记录仍投影到只读的“Agent 执行”终端。文件打开、目录展开和保存持续复用 Remote Workspace Transport 的 `interactive` lane，目录树预取使用独立的 `prefetch` lane；两条 lane 依附于同一认证 SSH client，但分别运行 Python 标准库 Worker，不会让慢目录扫描阻塞文件打开。Worker 源码由 Desktop 在 SSH exec channel 建立后通过 stdin bootstrap，完成握手后同一 stdin/stdout 承载带 `request_id` 的 framed RPC；服务器不需要开放新端口、安装守护进程或运行 Web 服务。多数已预取目录在首次展开时直接显示，尚未预取的目录仍按需读取。预取最多深入 5 层、读取 192 个目录与 5000 个条目，以 4 路并发和 15 秒调度预算约束成本；`.git`、`node_modules`、`.next`、`out`、`build`、`dist`、`.venv`、`venv`、`__pycache__` 及平台缓存等重目录保留入口但不递归，符号链接目录也从不递归。选择普通文件后会建立独立 Monaco model 与编辑标签；继续打开文件不会关闭已有标签，每个标签分别保存内容、revision、未保存状态、撤销栈、光标和视图位置。Monaco 按路径选择语言高亮，并提供查找、多光标和三态主题；使用“保存”或 `Ctrl+S` 只写回当前标签。断开或切换 SSH 目标时不会把旧文件内容写入新目标，未保存标签会保留为只读以便复制。

资源管理器顶部可在“远程工作区 / 本地工作区”之间切换，不新增活动栏入口。本地工作区默认尝试采用已配置的 Code Agent 工作目录，也可通过目录选择器显式授权另一目录；Main Process 按窗口持有随机 grant 与规范根路径，Renderer 只提交 grant 和相对路径，不能用 IPC 任意读取本机绝对路径。SSH 断开不影响已经授权的本地工作区。远程与本地标签分别保留；切换来源不会把本地内容写入远端，目标真正变化时仍按 revision 规则处理未保存文件。

本地文件打开后会把 FileHandle 的设备号/inode 与授权根内当前路径重新比对，读取和保存均使用该已验证句柄。保存通过句柄二次核验 revision，写入后 `fsync` 并回读；Windows 临时占用错误会在恢复原字节后有界重试，最终失败仍经同一句柄恢复原内容。不依赖 Windows 覆盖 rename，也不会因父目录在校验后被换成 symlink 而写到根外。Windows/libuv 不支持目录 FileHandle 时，目录枚举以两份有界快照逐项比对名称、类型和设备号/inode，验证后才返回文件树或加入搜索队列；普通文件仍要求句柄。进程或系统在原位写入与回滚之间崩溃时可能需要从编辑缓冲或版本控制恢复，当前没有本地事务日志。

文件树上方内嵌文本搜索与替换。搜索限制为 300 个可见结果、10000 个文件、单文件 1 MiB 和 10 秒，并跳过二进制、大文件、重目录与越界符号链接；本地扫描还限制目录数、单目录条目数和累计入队量，宽目录使用流式枚举。每次搜索有 request ID、取消信号和页面代际栅栏，迟到结果不会覆盖新查询。远程搜索使用独立的 `explorer-search` Workspace lane，避免阻塞文件打开。本地搜索首版只支持普通文本与大小写选项，不在 Electron Main Process 执行用户正则。结果携带文件 revision，点击可定位到 Monaco 行列；“全部替换”在结果截断时禁用，提交前重新核验每个唯一文件的 revision，逐文件以 CAS 保存。多文件替换不伪装为跨文件原子事务：若后续文件并发变化，平台停止并明确报告此前已经保存的文件数。

目录树在连接存续期间每 10 分钟自动刷新一次；计时从上一轮结束后重新开始，因此慢服务器不会叠加多轮扫描。每轮强制刷新当前已展开目录，折叠目录再次展开时若缓存已过期则先显示旧内容、再在后台更新。刷新采用 stale-while-revalidate：读取期间继续显示上一次成功结果，临时失败也不清空可见树；成功后原位替换缓存，保留已展开目录、键盘焦点、选中文件和编辑缓冲区。任一 Worker lane 出现超时、协议错误或 SSH channel 关闭时，平台只淘汰对应 lane，下一次同角色操作按需重建；交互 lane 故障不清空目录缓存或编辑缓冲区，后台 lane 故障也不阻塞文件打开。只读的 stat/list/read/search 可在重新核验目标后安全重试；写入和命令一旦已经发送，就不会通过 SFTP 或 direct exec 自动重放。Worker 尚未接收请求前的启动、握手或能力失败只允许只读操作和尚未发送的命令进入兼容降级路径，保存始终要求具备跨进程 revision 锁的 Worker。缓存、lane 和迟到结果绑定由主机、端口、用户名、工作目录与 SSH generation 组成的目标身份，断连或切换目标即失效；目录删除还会写入单调的子树 tombstone，防止较早启动的预取复活已删除缓存。手动刷新同样不先清空树，若前一轮正在运行则等待合并的后续轮次实际完成；文件保存后会立即刷新其父目录，不必等待下一轮后台扫描。

文件能力以配置的 SSH 工作目录为安全边界。Worker 在每次文件操作前解析真实路径；相对路径中的越级访问、解析后落到边界外的路径，以及指向工作目录外的符号链接都会被拒绝。编辑器只打开不超过 1 MiB 的有效 UTF-8 文本；二进制文件和更大文件继续使用 SSH Terminal 或服务器侧工具处理。编辑保留 UTF-8 BOM 与 LF/CRLF/CR 换行格式；混合换行文件以只读方式打开，避免静默改写。读取返回内容 revision；保存必须提交预期 revision 或明确创建新文件，Worker 使用同目录临时文件、落盘同步与原子替换，发现并发修改或删除时停止提交并要求重新加载。Monaco 0.53 与通用 Worker 都从应用包本地加载，CSP 只允许本地脚本和 Worker；不从 CDN 获取编辑器代码。Markdown 文件可在源码和安全预览间切换，复用既有 `MarkdownReport` / `MermaidDiagram`，不执行原始 HTML、不隐式保存，并对预览和 Mermaid 复杂度设置预算。文件树右键提供重命名、复制路径、复制/粘贴、下载和预览；每个操作携带页面当前可见的 SSH target、规范工作根与连接代次，Runtime 在发送任何 Worker 帧前将其核对并冻结为 client/generation/root，旧页面操作不会跟随新连接执行。下载在 Save 对话框前后各核验一次该身份，再使用远端快照与 512 KiB 分块传输，完整写入临时文件后提交；远端、SSH 目标或本地目标变化时停止并清理。`remote_exec` 的权限仍等同于当前 SSH 账户，不把工作目录文件围栏描述为命令沙箱。编辑中关闭或重载工作台时，应用会询问是否放弃未保存内容。

## 工作区布局

0.1.75 保留固定活动栏、中央功能区与右侧全高 Code Agent。SSH Terminal 位于中央功能区底部，可拖动高度或从面板标题折叠；助手与终端跨页面保留，切换负载采样、负载特征、寻优校准、并行建模、Trace 管理或本地设备时继续显示原会话。“让助手生成计划”会留在采样页，并展开右侧助手。点击活动栏的资源管理器图标会进入独立服务器工作区；重复点击已经激活的图标仍保持完整文件树和编辑器。树与编辑器之间的分隔条可拖动、用键盘微调或双击恢复默认宽度，尺寸按本机用户保存。

活动栏保持 50px 固定宽度；拖动助手左边缘或终端上边缘可调整内容区。助手初始宽度约为窗口的 40%，最大可扩展到 900px；终端初始高度约为可用区域的三分之一。选中分隔条后可用方向键微调，双击恢复该区域默认尺寸。页面内第二顶栏已经移除；渲染器标题栏中的“布局”菜单统一提供“打开 / 关闭助手”“打开 / 关闭终端”和“重置布局”。收起面板保留连接与进程。尺寸和折叠状态自动保存在本机，窗口缩小时自动限制尺寸以保留中间页面。

工作台整体可使用 `Ctrl++` 或 `Ctrl+=` 放大、`Ctrl+-` 缩小、`Ctrl+0` 恢复 100%；数字键盘的加、减和 0 同样可用，macOS 也支持对应的 `⌘` 组合键。快捷键在页面输入框、远程编辑器和 xterm 获得焦点时仍然生效。缩放比例保存在当前用户的桌面数据目录，重启后继续使用。

标题栏把应用图标、“编辑”“视图”“主题”“布局”“连接配置”排在同一行，应用图标位于“编辑”左侧，菜单文字随 `Ctrl++` / `Ctrl+-` 与页面同步缩放；不再显示产品名和版本号。点击菜单按钮或把鼠标停留 140 ms 都会打开同一原生下拉菜单，短延迟用于避免鼠标经过时误触。菜单根据标题栏按钮的窗口内容区坐标打开，把 CSS 坐标按当前缩放换算为 Electron 使用的内容区坐标，并在多屏和窗口边缘进行可见区域约束。Windows 右侧最小化、最大化和关闭按钮仍由系统绘制，保留 Snap Layout 等原生窗口行为。“主题”子菜单提供“深色”“浅色”和“白 + 黑”。“白 + 黑”将页面、资源管理器和下方 SSH Terminal 切为浅色，右侧全高 Code Agent 保持深色。切换只更新现有终端色板，不停止 Agent、SSH 或重建会话；浅色 palette 明确覆盖标准与高亮 ANSI 颜色，避免 Code Agent 或 SSH 输出沿用白色前景。选择保存在当前用户浏览器数据中并在启动绘制前恢复。文件树目录使用纯 CSS chevron，并以统一边长、旋转中心和垂直微调保持折叠与展开状态的光学居中。

SSH 面板使用 VS Code 风格标签栏。交互标签共享当前认证连接，但各自对应独立的远端 Shell 和 `terminalId`；关闭一个标签只关闭该 Shell，最多同时保留 8 个。固定的“Agent 执行”标签为只读事件视图，记录 Agent `remote_exec` 的命令、stdout、stderr、退出码和耗时，不参与交互 Shell 的输入与尺寸协商。面板不显示连接提示页脚、断开或重新连接按钮。保存 SSH 配置负责切换目标；断线后点击“+”负责恢复连接并创建终端，避免把连接生命周期作为常驻页面操作暴露给用户。

## 应用流程与机会推理

“应用流程”的真实 Trace 选择器使用一个目录呈现两类来源。第一类是当前服务器 Profile 中已经物化的 `trace_analysis_result`，包括原始 HTrace 与外部分析报告；第二类是 Windows 本机 Trace 管理中已登记归档的可分析步骤。选择本机步骤后，Desktop 才将对应 `capture.zip` 通过受限 SFTP 暂存通道上传，服务端校验归档并物化事件、阶段和热点，再绑定当前代码仓并刷新服务器 Trace 目录。选择器不再按当前代码仓提前隐藏历史 Trace；来源不一致时由后续源码绑定显式说明，不把 Trace 静默丢弃。

“真机推理/机会推理”接受三类输入，并把它们收敛到同一套硬件中立路由与证据边界：

| 输入 | 用法 | 证据边界 |
|---|---|---|
| 应用流程重建结果 | 当前只列出已完成真实 Trace 校准的场景；显式选择一个流程 stage 后，按其运行时 stage ID 过滤热点 | 未校准场景不进入该入口；没有映射到所选阶段的热点不会混入分析 |
| 真实 Trace 热点 | 从本机或已物化 Trace 提取热点，选择事件后核验代码仓中的源码 | 降级 Trace Span 不是 CPU self-time；候选源码不是已核验绑定 |
| 用户粘贴热点代码 | 提交代码及可选函数提示、事件名，直接形成可审计代码工件 | 标记为 `user_paste/unbound`，不伪造源码 revision、调用频度或性能占比 |

真实 Trace 输入沿用 Web 平台已经形成的四步方法：

1. **目标设备与系统画像**：沿用负载采样页当前选择的本机 HDC 设备，自动读取厂商、型号、系统版本、芯片和 CPU 核组；没有可用设备时仍允许分析真实 Trace，但硬件路由明确标记设备证据缺失。
2. **真实 Trace 与热点提取**：从服务器完整真实 Trace 工件目录选择记录，不以当前代码仓过滤。Raw HTrace 复用 Web 的父子调用栈直接子区间扣减、HiTrace Tag 解码与模块分组；页面将结果标为插桩 Span 排他时间估计，并展示质量与模块摘要。转换数据不可用时只显示原 Trace Span 指标，不能解读为 CPU self-time。其他 Trace 按总时长、最大时长、调用次数和阶段上下文恢复并排序热点。
3. **热点代码匹配**：优先核验 Trace 中声明的文件与行号；证据不完整时，在用户明确选择的代码仓快照内执行有界、字面符号检索，输出源码位置、片段和匹配层级，不把未命中包装成确定定位。
4. **模型语义路由与机会推理**：平台只从持久化源码匹配工件构造硬件中立画像和全部能力卡，不生成静态分数或候选单元。右侧 Code Agent 先仅依据隔离的 `model_input_context` 完成 NPU/GPU/DSP/媒体路由，再读取 Trace/源码审计证据和主路由 API 目录，连同 CPU 并行、SIMD 与算法方向形成机会。设备支持、正确性和性能收益仍通过后续构建与真机实测确认。

应用流程入口会读取完整场景定义，把所选 stage 的名称、顺序、源码锚点和映射后的运行时 stage ID 一并带入任务上下文；热点列表只显示其 `stage_context` 属于该映射集合的记录。当前入口要求场景已经绑定真实 Trace，因此未校准流程仍留在“应用流程”页用于流程理解与补证，不能被当作阶段热点输入。

三类输入最终都形成显式 code evidence，并与本机设备画像、硬件能力卡和 API 目录共同进入 `model_input_context`。输入来源和证据强度随结果持久化；路由命中不代表设备支持，机会判断也不代表性能收益。

## Desktop-first 与服务器环境

Desktop-first 下，Windows 管理工作台、Code Agent、本机 HDC、负载采样、Trace 原件、真机执行和本机 Agent 运行证据；Linux 只需要提供大仓源码访问、代码索引、自建 API 扫描、SDK/工具链编译和 Linux 专用 TraceStreamer 等能力。已有 Web/API 远程采集兼容链路仍可用于浏览器工作流，但 Desktop 已经直连的手机无需再经过远程采集端。

0.1.63 的 Platform RPC 在一个已认证 SSH client 内分别保留页面请求与 Agent 工具两条长驻 Python channel。每类连续请求复用自己的解释器和模块导入，页面刷新不会排在 Agent 流程工具的同步队列之后；切换 SSH target/profile、Worker identity 或 generation 时关闭相关通道，迟到结果不会进入新目标。每条远端 dispatcher 仍同步执行，取消或超时会关闭该通道并让下一次请求重建，因此它们都不是可恢复后台任务。迁移页面当前仍读写服务器 `HETERO_STATE_ROOT`；后续 local-first 会把 Trace 索引、硬件/校准模型、建模、流程 registry、推理工件、Skill 和 Experience 状态收回 Desktop Local Platform Host，只把源码、构建和 Linux 工具操作路由到服务器。

本地 Code Agent 的 `HETERO_AGENT_SKILL_ROOT` 包含工作台工具、应用流程重建和 DPVS / CVS 性能分析三个受管 Skill。Desktop 从版本化发布资源计算精确 manifest，拒绝符号链接和未允许的文件类型，核对文件数量、大小与 SHA-256，并在用户数据目录生成只读、按 identity 寻址的副本；目录多出、缺失或内容变化都会重新物化并复验。仍引用旧实验工具的 experimental flow Skills 不作为本地可执行能力暴露。

先连接 SSH，再打开“服务器环境”。用户需要填写以下远端资源：

除 Python 可以填写 `python3` 这类简单 PATH 命令外，资源目录和可执行文件统一填写以 `/` 或 `~/` 开头的远端路径；相对路径会在保存或预检前被拒绝。路径可以包含空格和中文。

| 页面字段 | 环境变量 | 要求 |
|---|---|---|
| 环境名称 | Desktop `profileId` | 决定当前 SSH 目标上的远程托管根；不参与 SSH 目标作用域计算 |
| 异构使能工作仓 | `HETERO_REPO_ROOT` | 包含本平台仓库、Build Worker 和版本化工具；它不是待分析产品源码 |
| 代码分析源码根 `sourceRoots[]`（可多项） | 无单一环境变量 | 供 Agent 检索与流程分析；后续进入 Source Connector registry，由任务显式选择 |
| OpenHarmony 头文件资源根 | `HETERO_OPENHARMONY_SDK_ROOT` | 同一根下同时包含 `interface/sdk_c` 与 `third_party` 的规定能力面 |
| LLVM bin 根目录 | `HETERO_LLVM_BIN_ROOT` | 包含可执行的 `clang`、`clang++`、`ld.lld`；同一工具链根下的 `lib/clang/<version>/include` 需包含 `stddef.h`、`stdint.h` 等 Clang 内建头文件 |
| glslang 可执行文件 | `HETERO_GLSLANG_BIN` | Vulkan shader 编译器的完整文件路径 |
| TraceStreamer 可执行文件 | `HETERO_TRACE_STREAMER` | Linux 版本的 TraceStreamer 完整文件路径 |
| OpenHarmony 运行库根目录 | `HETERO_RUNTIME_LIBRARY_ROOT` | 集中放置目标 AArch64 `.so`，无需按 profile 建子目录 |
| Python 可执行文件（可选） | `HETERO_PYTHON_BIN` | 服务器分析与构建脚本解释器，默认 `python3`，也可填 PATH 名称或路径 |

“OpenHarmony 头文件资源根”不是只有 EGL/OpenGLES/Vulkan 的普通 third-party 目录。当前编译面还需要 AI、Graphic、Multimedia、musl NDK include、musl AArch64/generic 以及 `alltypes.h.in`。如果手头只有完整 OpenHarmony checkout 或分散的 third-party 来源，可以用“交给 Agent 补齐”让已启动的 Agent 检查来源并协助生成紧凑 bundle；页面实际保存的仍是最终 `openHarmonySdkRoot`，不会保存另一套含义含混的 third-party 路径。

平台托管路径不要求用户逐项填写。每个服务器 profile 使用：

```text
$HOME/.local/share/heterogeneous-workbench/server-profiles/targets/<target-scope>/<profile-id>
```

`<target-scope>` 是平台根据规范化的 `host + port + username + cwd` 生成的 16 位 SHA-256 前缀，不需要用户填写。同一账户下的不同工作目录即使都使用 `default` 环境名，也不会覆盖彼此的状态和 `platform.env`。“初始化平台目录”在该根下创建 `state/`、`cache/`、`assets/`、`tmp/`、`logs/` 和 `run/`，并把 `HETERO_USER_HOME`、`HETERO_ROOTLESS_RUNTIME_ROOT` 指向 profile 根，把 `HETERO_STATE_ROOT`、`HETERO_CACHE_ROOT`、`HETERO_RUNTIME_ASSET_ROOT` 与 `TMPDIR` 派生到对应子目录，最后以权限 `0600` 写入 `platform.env`。初始化不下载工具、不覆盖用户目录、不修改系统配置。“检查环境”是只读操作，会分别报告路径存在性、文件类型、执行权限、SDK 清单和运行库能力；“交给 Agent 补齐”只把明确的缺项与目标路径提交给当前 Code Agent，实际更改仍可从 Agent 输出和远程文件中审阅。

本地配置按规范化的 `host + port + username + cwd` 四元组分开保存。连接另一台服务器、另一个账户或同一服务器的另一工作目录时，页面自动加载该目标的 profile；首次出现的目标使用空默认值，不继承上一目标的 SDK、源码或运行库路径。旧版单一 `serverProfile` 只会迁移给当时已保存的 SSH 目标；没有旧 SSH 目标时，只迁移给之后首个配置的目标。预检和初始化请求也携带同一四元组，目标在执行期间改变时会停止后续写入。

0.1.41 的通用 SSH Terminal 和 Agent `remote_exec` 不会自动加载这份 `platform.env`。当前手工调用 Build Worker 时应先显式 source 该文件；三个共享分析页的受限 Desktop transport 会针对每次请求加载当前目标 profile，并只调用白名单分析操作。请求携带当前 `host + port + username + cwd` 目标和随机请求 ID；Main Process 在执行前后核对目标，页面取消时关闭本次 SSH exec channel。保存了另一目标的 profile、连接在执行中切换或作用域不匹配时，操作会明确失败，不会把旧目标的数据写入新服务器。

“负载特征”与 Web 复用同一个 `WorkloadFeaturesStudio`、特征展示函数和 CSS。连接 SSH 并保存服务器资源配置后，页面可读取服务器托管状态中的数据集与特征版本、载入内置图库归档示例、提取阶段特征，并导出 JSON/CSV。若当前目标尚未创建受管 `platform.env`，首次分析会自动初始化目标隔离的托管目录并重试；若配置保存后 Worker 仓库或 Python 已改变，Desktop 会识别过期 `platform.env`，按当前 Profile 重建后自动重试。每次远端 RPC 都从 Profile、SSH 工作目录及其 Git 根目录发现 `backend.tools.desktop_platform_rpc`，并验证配置的 Python、仓库 `.venv` 与 `venv` 中已有的解释器，避免把仓库包误判成需要安装的第三方包。并发首屏请求只在 SSH 目标和完整 Profile 配置都一致时复用同一个初始化任务。尚未填写异构工作仓目录时，页面会指向“连接配置 → 服务器环境”，无需用户手工创建或编辑 `platform.env`。负载采样完成后，负载特征页的统一“选择负载数据集”会同时列出本机 Trace 登记中的可分析步骤、尚未物化的内置历史归档和既有归档数据集；本机步骤使用“`[计划名称] Step N: 步骤名称`”展示来源。点击“提取负载特征”后，本机归档通过目标围栏保护的 SFTP 上传到当前 profile 的托管暂存区，服务端校验大小、SHA-256、ZIP 清单和逐文件哈希，再一次导入 Trace、PMU、HiLog 与元数据并为有效步骤创建数据集；内置历史归档则按需物化。Trace 管理中的“负载特征分析”执行同一闭环；多步骤归档先明确选择步骤，再自动打开对应的新特征版本，不再要求分别导入 Trace 与 PMU。补充测量 JSON 在当前短命令通道中限制为 16 KiB。Desktop 暂不显示该页 Web 版的“一键助手分析”按钮，因为专用 workload MCP 尚未迁入本地 Agent 工具；右侧全局 Code Agent 仍可手动使用。

0.1.48 的 Desktop 发布物还携带与自身版本绑定的最小分析 Worker。构建时只收集负载特征、校准和并行建模 RPC 所需文件，为每个文件记录相对路径、字节数与 SHA-256，并对版本和文件清单再计算整体 identity；应用在建立远端部署通道前先核验本地资源。现有仓库和虚拟环境均无法提供兼容 RPC 时，Desktop 才把这份 Worker 上传到当前 target/profile 的 `assets/analysis-worker/.partial-*`，在服务器逐文件复核 manifest、拒绝符号链接和额外文件，再以目录 rename 原子激活到 `assets/analysis-worker/<identity>`。文件完整性与运行就绪性分开判断：只有版本目录缺失或内容损坏才上传；Python 不可执行、低于 3.10、依赖无法导入或 RPC schema 不兼容时保留已经通过完整性校验的版本，后续只复检环境，不重复传输。0.1.51 起所选 Python 需实际导入 `desktop_platform_rpc/v2` 后才重试原分析请求；相同 identity 已就绪时直接复用，同一进程内请求合并，多个 Desktop 客户端则通过远端 identity 锁串行激活并在锁内复验最终目录。

托管 Worker 是 Desktop 自身分析合同的版本化运行资产，不会覆盖、更新或清理用户的异构工作仓和产品源码，也不会执行 `git pull`、`sudo` 或修改系统 Python。代码底图与应用流程复用一套显式管理的 Python 分析环境：仅当 RPC 返回 `CODE_GRAPH_EXTRACTOR_UNAVAILABLE` 或 `DESKTOP_ANALYSIS_RUNTIME_UNAVAILABLE` 时，Desktop 才在当前 target/profile 的 `assets/code-analysis-runtime/tree-sitter-c-cpp-jsonschema-v2/` 创建独立虚拟环境，按锁定版本安装 `tree-sitter`、`tree-sitter-c`、`tree-sitter-cpp` 与 `jsonschema`，完成 C/C++ 实际解析自检后重试原请求。安装使用服务器既有 pip 源、代理和 CA 配置，缓存与安装日志都留在该 profile；不会写入系统 site-packages、用户仓库或其他 profile。上传或校验失败会清理本轮 `.partial-*`，既有有效版本继续保留。远端探测使用的 `HETERO_DESKTOP_REPAIRABLE`、Worker root/ready 等内部 marker 由 transport 解析，不作为错误正文展示；用户看到的是自动准备结果或正常的可执行诊断。只有托管部署和安全重试仍失败时，页面才提供脱敏后的 Code Agent 修复入口。

上述发现、托管 Worker 部署、配置重建和重试仍不能恢复时，分析错误区提供“使用 Code Agent 修复”。提交内容包含当前页面、RPC 目标、脱敏后的 SSH/Profile 信息和错误正文，不包含密码、私钥、Bearer/Basic 凭据或 token；Code Agent 使用当前 SSH 工具诊断并在同一目标上验证。平台的无人值守修复边界限定为发现现有仓库和解释器、部署随版本校验的 Worker、重建平台托管的 `platform.env`、按上述固定合同准备 Tree-sitter 解析环境，以及重试同一请求；不会根据任意错误文字盲目安装包、执行 `git pull` 或 `sudo`、修改系统 Python 或覆盖用户仓库。

“寻优校准”复用 Web 的 `OptimizationCalibrationStudio`。当前可查看模型结构和参数目录，创建、读取并下载校准方案，导入不超过 20 MiB 的实测 JSON 完成拟合与质量检查，并把通过检查的拟合发布为模型版本。实测内容不进入短命令输入：Desktop 先绑定当前 target/profile 和方案申请一次性预约，再以 typed SFTP 上传到受管暂存路径；本机与服务端分别核对 UTF-8 字节数和 SHA-256，拟合或异常结束后清理文件与预约。任务类型核频方案对每核组/类别至少使用 5 个训练 OPP，并另留 1 个未见 OPP；硬件 min/max 进入训练集，发布曲线只在训练频率闭区间内消费，域外回退旧弹性并记录原因。已有校准数据和版本仍沿用同一服务端目录。外部 OPP 文件链接不在 Electron 中暴露。

“并行建模”复用 Web 的 `ParallelModelingStudio`。“负载归档”直接列出当前服务器 Profile 中全部可分析数据集，包括本机 `capture.zip` 形成的采集负载、Trace/解析结果导入负载和历史归档；尚未物化的随版本样例列在同一目录，载入后按 `provenance.builtin_id` 去重。负载特征页每次跳转都以新的 handoff revision 原子应用 dataset 与 feature profile，即使重复带入同一负载也会覆盖建模页中的临时选择。选定负载后可直接执行真实分析与搜索，查看组件账目、实测/候选依赖图、最优配置的预测 DAG 与时序、核心组合推荐、候选配置和已保存历史，并在“负载特征—校准—并行建模”之间传递 dataset、feature profile 和 model ID。Desktop 通过一次 SSH 请求同步执行，适合当前可在 120 秒内完成的分析；Web 的异步 job/SSE 没有冒充为已迁移能力。非最优配置的 DAG/逐节点时序、超过 100 条的候选和完整结果导出等待 typed job 与通用 Artifact 通道。服务端仍保存完整结果，Desktop 当前回传页面所需图形与有界比较数据。

RPC 请求命令限制为 32 KiB。JSON 响应原文最大 8 MiB；超过 512 KiB 时服务端使用 gzip+base64，在 768 KiB wire 上限内返回，以覆盖真实全 PMU 特征而不触发 `remote_exec` 的 1 MiB stdout 上限。压缩后仍超限时返回结构化错误，不截断成看似完整的结果。

Trace 采集本身在 Windows 执行，不依赖 Linux TraceStreamer。需要解析 `.htrace` 时，现有路径由 Linux Worker 使用 `HETERO_TRACE_STREAMER` 处理显式上传的副本；Linux 路径不能直接当作 Windows 路径。服务器环境页按 profile 白名单检查运行库文件，并分别核对 registry 是否覆盖该 profile 的全部库及当前 SHA-256；因此完整的 GLES 登记不会被误当成 NPU 已就绪。“初始化托管目录”会调用现有 Build Worker 登记工具，把共享目录及逐文件身份写入托管 `state/build-worker/runtime-library-source.json`，但不会复制或改写用户提供的 `.so`。GPU、NPU 和媒体 profile 共用物理目录，库内容改变后必须重新登记。

Agent 的 `remote_exec` 仍是有界的短命令能力：它复用长驻 `agent` lane，默认 30 秒、最长 120 秒，stdout/stderr 合计上限 1 MiB。Worker 在命令发送前无法启动或握手时可降级到普通 SSH exec；命令已经发送但结果未知时不会重放。该工具适合环境检查和轻量操作，不用于长时编译、索引或大文件交付。后续将引入带 `job_id`、状态对账、取消和 artifact manifest 的 typed remote job；产物通过 SFTP 临时文件传输并核验 `bytes + SHA-256` 后原子发布。该能力尚未完成，当前需要长任务时继续在可观察的 SSH Terminal/tmux 中执行并人工核对结果。

## 接入自研 Code Agent

以团队 CLI 为例（参数名称以你自己的软件为准）：

```json
{
  "executable": "C:\\Tools\\TeamAgent\\team-agent.exe",
  "args": ["--workspace", "{workspace}", "--mcp-config", "{mcp_config}"],
  "cwd": "C:\\Workspaces\\agent-workbench",
  "environment": {},
  "submitMode": "bracketed_paste_cr",
  "startupDelayMs": 1000,
  "submitDelayMs": 1000
}
```

可复制 [`examples/team-agent.windows.json`](examples/team-agent.windows.json)。如果你的 CLI 没有 `--workspace` 或 `--mcp-config` 参数，请按其真实接口修改；只有可执行程序能启动，并不等于它已加载 MCP 工具。

- **MCP 接入**：“工具集成”提供标准 `mcpServers` 配置路径。支持自定义 MCP 配置的 Agent 可通过 `{mcp_config}` 参数读取。Agent 采用其他配置格式时，将该 server 条目接入其原生配置即可。
- **CLI 接入**：具有 Shell 工具的 Agent 可以调用环境变量 `HETERO_DESKTOP_TOOLS` 指向的工具程序，例如在 PowerShell 中执行 `& $env:HETERO_DESKTOP_TOOLS remote_exec 'pwd && git status --short'`；macOS/Linux 使用 `"$HETERO_DESKTOP_TOOLS" remote_exec 'pwd && git status --short'`。
- 工具 `remote_status` 返回当前 SSH 连接；`workspace_stat`、`workspace_list`、`workspace_read`、`workspace_search` 和 `workspace_write` 通过 `agent` lane 访问远端工作区，写入使用 revision 防止覆盖；`remote_exec` 通过同一 Remote Workspace Transport 返回 `stdout`、`stderr`、`exitCode` 与耗时。命令在配置的远程目录执行，终端中临时 `cd` 不改变 Agent 的工作目录。
- 软件内置 Node 运行环境用于工具适配器；最终用户不需要单独安装 Node 来运行这些工具。工具会话令牌通过 Agent 进程环境继承，不写入 MCP 配置或命令行参数。
- `.cmd` 启动入口经过 Windows `cmd.exe` 适配；遇到包含 CMD 展开字符的复杂参数，可选择真实 `.exe`，或 `node.exe` 配合 CLI 的 `.js` 入口。
- 桌面原型复用 manifest 的可执行文件、argv、环境变量与提交方式；0.1.63 已为应用流程提供本地受管 Skill，Web 平台的完整 Run、原生 Session 恢复、通用动态 Skill 投影、模型覆盖和用量采集仍未迁移。

推荐给 Agent 的首个验证任务：

> 使用 desktop-ssh 的 remote_status 和 remote_exec 工具，检查当前服务器工作目录、操作系统及项目 Git 状态。汇总执行结果。

未接 MCP 时：

> 调用环境变量 HETERO_DESKTOP_TOOLS 指向的工具，先执行 remote_status，再用 remote_exec 在服务器检查 pwd、uname -a 和 git status --short。

## 负载采样与 Trace 管理

桌面采集页与 Web 平台采用相同的信息顺序：先定义“场景与采集步骤”，再选择“采集器与设备”，随后执行或保存计划，并在下方查看“已保存计划”和“采集结果”。桌面仍使用本机 HDC 与固定 Runner，页面对齐不改变计划、设备冻结和归档合同。

1. 在“本地设备”配置 `hdc.exe` 路径，或使用 PATH 中的 `hdc`。在“负载采样”刷新设备并明确选择手机；多台手机不会自动切换目标。
2. 使用“图库 17 步”模板，或编辑自定义步骤、应用信息和控件选择器；勾选需要测量的步骤。其余前置操作按计划执行。
3. 选择 Trace、HiLog、PMU。桌面新计划默认 PMU 周期 10 μs；这是请求采样周期，实际覆盖、事件组复用及手机支持仍以产出数据为准。可选自动准备或已准备模式。
4. 保存计划后执行。App 在本机启动固定 Runner，执行前冻结计划、设备序列号与 HDC 路径；界面显示准备、步骤和归档进度。取消时 Runner 清理本轮采集器并保留已有结果。
5. “Trace 管理”查看本地记录、步骤清单与文件位置，可登记现有采集目录或标准 `.capture.zip`。移除记录仅移除登记，原始文件由你在资源管理器管理。
6. 用“打开归档”定位 `.capture.zip`，或直接点击“负载特征分析”。Desktop 会把完整归档安全上传到当前 SSH profile 的托管目录，为可分析步骤创建数据集、提取特征并进入对应结果；原始数据仍保留在 Windows。

“让 Agent 生成计划”会将当前配置、操作描述和设备快照提交给已启动的真实 Code Agent；Agent 通过 `capture_catalog` / `capture_save_plan` 保存计划，界面自动更新。生成计划与执行采集是两个动作，可先检查新计划再执行。采集本身不依赖 Agent 或 SSH 在线。

工具目录新增 `device_discover`、`capture_catalog`、`capture_status`、`capture_save_plan`、`capture_start`、`capture_cancel`、`trace_list`、`trace_detail`。CLI 与 MCP 使用同一合同。Windows 下复杂 JSON 建议放到 UTF-8 文件中再传入，避免 PowerShell 引号转义：

```powershell
& $env:HETERO_DESKTOP_TOOLS capture_catalog
& $env:HETERO_DESKTOP_TOOLS capture_save_plan '@C:\Workspaces\plan-request.json'
& $env:HETERO_DESKTOP_TOOLS capture_status
```

`plan-request.json` 为 `{"plan": {...}}`，内部字段以 `capture_catalog` 为准。开始采集使用 `{"planId":"...","deviceSerial":"..."}`；序列号必须等于 App 当前选择。工具不会根据发现顺序替换所选手机。


## 从源码运行与构建

频繁更新推荐源码运行：准备 **Git for Windows、Node.js 24 或更新版本的 Windows x64 发行版（含 npm）**，打开新的 PowerShell，进入本仓库根目录。运行中的旧 App 先退出，正在采集时先等待完成。

首次获取新入口（已有仓库）：

```powershell
git pull --ff-only
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1
```

没有仓库时先 `git clone <本仓库地址>` 并进入目录。私有仓库使用自己的 Git 认证；SSH 服务器连接配置与 Git 认证分别管理。

以后更新并打开 App 只运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update
```

脚本拉取代码、准备运行环境、重建界面及采集运行时，执行无手机的计划保存/生成包自检后启动桌面窗口。拉取失败会停止并显示 Git 原始错误，不清理你的源码改动。首次会下载 npm 依赖和 Electron 本体；未指定本机 Python 时，还会下载经 SHA-256 校验的官方 Python。后续普通代码和版本号更新复用缓存。依赖锁、Node 运行环境变化或关键 npm 包缺失时重装依赖。Electron 通过显式准备步骤安装和验证，缺少 `path.txt` 或本体无法运行时单独修复，下载失败后重试保留已安装的 npm 包；内置 Python 文件损坏时从缓存包恢复，缓存包也损坏才重新下载。

源码运行与安装版共用 `%APPDATA%\HeterogeneousWorkbench`，原来的 Agent、SSH、设备配置和采集记录保留。**更新源码不更新已安装的旧 EXE**；使用上述脚本启动新版本，不要再点旧版本快捷方式。运行期间保持 PowerShell 窗口打开，先正常关闭 App 再关闭窗口。

只构建和自检、不启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -BuildOnly
```

需要独立 EXE 时在 Windows 本机打包：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update -Package
```

输出位于 `desktop/release/`；打包工具首次运行也会下载所需资产。`-Package` 会忽略源码运行所选的外部 Python，准备并验证固定的 CPython 3.13.17 embeddable，再把它放入安装版和便携 EXE，因此发布物不依赖用户电脑上的 Python。仓库的 [Desktop Workbench 工作流](../.github/workflows/desktop-preview.yml) 在 Windows PowerShell 5.1 / PowerShell 7 分别验证本机 Python 源码构建、内置 Python 首次与缓存构建，使用实际选中解释器保存、修改计划并验证生成包，再运行桌面测试和打包。macOS 开发进入 `desktop/` 后用 `npm ci`、`npm start`（显式外部运行时支持 CPython 3.11–3.15，可用 `HETERO_DESKTOP_PYTHON` 指定路径）；`npm run dist:mac` 构建开发验证包，当前不分发 macOS Python。预置 `node-pty` N-API 二进制支持 Windows ConPTY；macOS helper 的执行权限在安装步骤自动修复。

### Windows 计划保存报 Permission denied

0.1.27 会在写完计划文件后打开父目录做 POSIX 风格同步，Windows CRT 拒绝目录句柄，错误路径停在 `capture\packages\plan-store\artifacts\scenario_capture_plan`。0.1.28 按平台区分目录同步，保留文件落盘和原子替换，并同步修复结果导入中的同类操作。更新后重新保存计划即可；不需要删除 AppData 或用管理员身份运行。真正的文件不可写、被占用等错误仍会原样报告。

### 源码启动报 no such file / electron/path.txt

Electron 45 的 npm 包不再通过 postinstall 下载运行程序；`npm ci` 成功不等于 Electron 本体已安装。0.1.28/0.1.29 的源码启动脚本提前读取 `path.txt`，在全新依赖目录会失败。0.1.30 起增加显式准备；0.1.31 使用官方 `@electron/get`、解压器及包内校验清单，在此基础上处理代理、重试与离线导入。核验程序可运行及版本、平台和架构后，再记录完整依赖状态并启动。

在仓库根目录执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update` 即可。不要手动创建 `path.txt`；它应在解压完成后生成。不需要清理 AppData 或删除 `node_modules`。

### 源码准备下载失败：先看失败阶段

源码首次启动有三条独立的下载链路。`npm config set registry ...` 只改变第一条，不会自动改变后两条：

| 报错阶段 | 下载内容 | 离线输入或企业镜像 |
| --- | --- | --- |
| `Dependency installation` | npm 依赖包 | npm registry / 本机 npm cache |
| `Electron runtime preparation` | `electron-v45.0.0-win32-x64.zip` | `HETERO_ELECTRON_ARCHIVE` / `HETERO_ELECTRON_MIRROR` |
| `Python runtime preparation` | `python-3.13.17-embed-amd64.zip` | `HETERO_PYTHON_ARCHIVE` / `HETERO_PYTHON_MIRROR` |

报错主机为 `www.python.org` 时，Electron 已经准备完成，当前失败的是后续 Python runtime。设置一个 Electron archive 不代表整个源码构建都已离线。

0.1.34 起，源码运行还可以通过 `HETERO_DESKTOP_PYTHON` 使用已安装的 Python，从而跳过表中的 Python 下载；npm 依赖和 Electron 仍按各自链路准备。

#### Electron runtime

`TypeError: fetch failed` 通常表示 Electron ZIP 下载请求失败，尚未进入 App。`electron@45.0.0` 的 npm 包只有安装入口和接口文件，真正的 Windows 运行时由独立地址提供。

0.1.32 将准备步骤作为 npm lifecycle 运行，自动继承 `.npmrc` 的 `proxy`、`https-proxy`、`noproxy`、`cafile` 及兼容的 `electron_mirror` 配置。传入后只保留下载所需字段，registry token 等 npm 配置不会继续传给下载进程。显式环境变量优先；`HETERO_ELECTRON_ARCHIVE` 离线模式跳过所有网络配置。

未配置企业 Electron 镜像时，下载器先访问 GitHub，网络失败时有限重试并回退到 `https://npmmirror.com/mirrors/electron/`。GitHub 最终下载主机通常为 `release-assets.githubusercontent.com`，npmmirror 最终主机通常为 `cdn.npmmirror.com`；企业代理、域名白名单和 CA 也需允许重定向后的主机。无论来自哪个源，都按锁定 npm 包内的 SHA-256 验证 ZIP。已明确设置镜像时只访问该镜像，不回退公共站点。

先退出旧 App，再执行通常的更新命令即可。若公司提供了 Electron 二进制镜像，其根目录必须能按标准结构提供 `v45.0.0/electron-v45.0.0-win32-x64.zip`。在当前 PowerShell 中指定：

```powershell
$env:HETERO_ELECTRON_MIRROR = 'http://your-company-mirror.example/electron/'
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update
```

`HETERO_ELECTRON_MIRROR` 是本项目的稳定入口，优先于 `.npmrc` 和通用 `ELECTRON_MIRROR`。内部 HTTP 镜像也可使用，因为 ZIP 仍会执行固定 SHA-256 校验；公网镜像保持 HTTPS。不要把 npm registry URL 直接填进来，除非管理员确认同一服务另行提供了上述 Electron 文件结构。

企业网络需要代理时，可以继续使用现有 npm `proxy` / `https-proxy` / `cafile` 配置，也可在同一 PowerShell 设置 `HTTP_PROXY` / `HTTPS_PROXY` / `NODE_EXTRA_CA_CERTS` 后重跑。直接环境变量优先。脚本不会自动读取浏览器 PAC 或 Windows 系统代理，且不会沿用 npm 的 `strict-ssl=false`；企业证书应通过 CA 文件接入，保留 TLS 验证。

如果浏览器可以下载、Node 下载仍失败，可手动下载固定版本 [electron-v45.0.0-win32-x64.zip（官方）](https://github.com/electron/electron/releases/download/v45.0.0/electron-v45.0.0-win32-x64.zip) 或[同版本镜像](https://npmmirror.com/mirrors/electron/v45.0.0/electron-v45.0.0-win32-x64.zip)，无需自行解压。在仓库根目录执行：

```powershell
$env:HETERO_ELECTRON_ARCHIVE = "$env:USERPROFILE\Downloads\electron-v45.0.0-win32-x64.zip"
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1
Remove-Item Env:HETERO_ELECTRON_ARCHIVE
```

离线 ZIP 按锁定校验值验证后才解压；错误版本或文件损坏会明确报错，原 ZIP 保留。该选项只替代 Electron 下载，npm 包和 Python 仍需要各自缓存或首次下载。诊断会区分下载、校验、解压及程序验证，并显示 `ENOTFOUND`、连接超时或证书等原因；重试保留已安装的 npm 包和已有配置。

#### 使用电脑上已安装的 Python

Windows 已安装 Python 3.15.2 时，在**仓库根目录的同一个 PowerShell 窗口**执行：

```powershell
$env:HETERO_DESKTOP_PYTHON = ((& py -3.15 -B -I -S -c "import json, sys; print(json.dumps(sys.executable, ensure_ascii=True))") | ConvertFrom-Json)
& $env:HETERO_DESKTOP_PYTHON --version
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update
```

第一条命令通过 Windows Python Launcher 取得真实解释器绝对路径；其中的 ASCII JSON 编码可避免 PowerShell 5.1 破坏中文用户名，路径包含空格也可以使用。如果电脑没有 `py` 命令，可直接设置安装路径，例如：

```powershell
$env:HETERO_DESKTOP_PYTHON = 'C:\Users\your-name\AppData\Local\Programs\Python\Python315\python.exe'
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update
```

该入口只接受 **CPython 3.11–3.15**。启动器使用 `-B -I -S -X utf8` 运行探针，禁止生成 `.pyc` 并隔离用户环境，核对版本、解释器真实路径、桌面采集所需标准库，再使用同一解释器完成构建后的 catalog、计划保存和采集包校验。所有检查通过后不再准备或下载内置 Python，也不需要设置 `HETERO_PYTHON_ARCHIVE`。显式路径不存在、版本超出范围、不是 CPython、标准库不完整或采集闭包自检失败时，源码启动会立即停止并保留具体错误；它不会静默切换到缓存或访问 `www.python.org`。

这个选项仅改变桌面采集的**源码运行**。执行 `-Package` 时，脚本会忽略该外部解释器，始终准备固定的 CPython 3.13.17 embeddable；安装版和便携版也只使用包内运行时，保持自包含。它不等同于 Web/FastAPI 后端支持 Python 3.15；后端仍按根目录部署说明创建自己的 `.venv` 并安装第三方依赖。

需要恢复内置 Python 源码运行时，可在当前 PowerShell 清除变量后重跑：

```powershell
Remove-Item Env:HETERO_DESKTOP_PYTHON -ErrorAction SilentlyContinue
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update
```

#### 内置 Python runtime

0.1.33 将 Python 准备步骤也改为 npm lifecycle，继承 `.npmrc` 中的 `proxy`、`https-proxy`、`noproxy` 与 `cafile`，再清除 npm 配置和 registry token 后调用下载器。直接设置环境时可使用 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` / `CURL_CA_BUNDLE`。企业 Python 镜像通过 `HETERO_PYTHON_MIRROR` 接入，根目录必须提供 `3.13.17/python-3.13.17-embed-amd64.zip`。显式镜像只访问该地址，不推测 npm registry 的文件结构。

网络受限时，下载官方 [CPython 3.13.17 Windows embeddable x64 ZIP](https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip)，保持 ZIP 原样且不要解压。其固定 SHA-256 为：

```text
9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38
```

在仓库根目录执行：

```powershell
$env:HETERO_PYTHON_ARCHIVE = "$env:USERPROFILE\Downloads\python-3.13.17-embed-amd64.zip"
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update
Remove-Item Env:HETERO_PYTHON_ARCHIVE
```

外部 ZIP 路径可以包含空格或中文。脚本先核对固定 SHA-256，再从该文件构建内置 Python；离线模式不读取代理或访问网络，也不移动、改写或删除原文件。校验或解压失败不会替换之前有效的运行时。首次成功后缓存可以复用，不必持续设置变量。

完全离线首次准备时，需要同时提供两个 ZIP：

```powershell
$env:HETERO_ELECTRON_ARCHIVE = "$env:USERPROFILE\Downloads\electron-v45.0.0-win32-x64.zip"
$env:HETERO_PYTHON_ARCHIVE = "$env:USERPROFILE\Downloads\python-3.13.17-embed-amd64.zip"
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update
Remove-Item Env:HETERO_ELECTRON_ARCHIVE, Env:HETERO_PYTHON_ARCHIVE -ErrorAction SilentlyContinue
```

这两个变量只替代运行时 ZIP；首次 `npm ci` 仍需要可用 registry 或完整 npm cache。

参考：[Electron 安装与镜像说明](https://www.electronjs.org/docs/latest/tutorial/installation)、[@electron/get 代理与缓存](https://github.com/electron/get)。

## 数据与生命周期

Windows 设置位于 `%APPDATA%/HeterogeneousWorkbench`，macOS 位于 `~/Library/Application Support/HeterogeneousWorkbench`。包括非秘密偏好、主机指纹、工具入口，以及 `capture/` 下的计划、任务和 Trace 库。SSH 密码只以操作系统 `safeStorage` 密文保存在独立的 `ssh-credentials.bin`，不写入 `settings.json`；私钥口令和工具令牌不保存。Agent 直接环境变量中的常见密钥字段不持久化，推荐 `{env:VARIABLE_NAME}` 引用本机环境。

0.1.63 在 `agent-runs/` 中保存本机 Code Agent 的有界真实交互证据：每个任务保留最多 8,000 字符的任务摘要和最多 8,000 字符、已清除 ANSI/控制字符的 PTY 可见输出尾部，同时记录首个可见输出时延、输出首末时间与字节数，以及经 Desktop Agent broker 发生的 Platform RPC、SSH 命令、耗时和错误。经验演进页可展开这些任务/输出片段并查看工具等待，但它们不是完整对话，也不等于模型内部思维；Agent 自身未上报的文件操作、厂商 CLI 内部阶段和没有可归因工具调用的 PTY 静默仍无法确定原因。既有服务器历史与本机证据分开呈现，后续再由本地 Experience Store 统一索引。

后续多窗口按三层状态管理：Global Host 共享安全凭据、连接 profile、HDC/设备租约、SSH 连接池、Trace/blob、硬件与校准模型、KB、Skill 和 Experience；Workspace 保存源码 revision、数据集与模型绑定、流程/推理/并行分析和对共享资产的引用；Window Instance 隔离当前页面、草稿、Agent PTY/token、任务、终端、打开文件、未保存编辑和布局。0.1.63 仍只支持一个窗口和一个 Desktop Runtime，第二次启动只聚焦现有窗口；多窗口将在拆分 Global Host 与 Workspace Session、把 broker token 按 instance 路由并补齐共享写入/设备租约测试后开放。

Agent 工具绑定启动时的 SSH 主机、端口、用户与工作目录；启动时尚未连接则在首次工具调用时绑定。同一目标的自动重连可继续使用；SSH 目标发生变化时，保存 Code Agent 配置或使用配置页的手动启动入口会建立绑定到新目标的 Agent 会话。替换 Agent 时会撤销旧工具凭据，旧进程不会静默跟随新服务器执行。

关闭软件会请求取消进行中的本地采集、终止本地 Agent、断开 SSH 并关闭工具入口；退出收尾超过 15 秒时结束本机采集进程并标记中断。异常退出后，残留任务在下次启动时标记为中断，保留诊断和已有文件，不自动重复执行。远端已自行脱离 SSH 的后台任务由服务器管理；交互终端中的长任务可使用服务器的 `tmux`。原型不自动恢复已运行的 Agent 进程，也不把连接断开后的结果标记为成功。

验证状态和已知边界见[桌面设计文档的验收记录](../docs/desktop_workbench_design.md)。0.1.26 的 Windows SSH、内部 Agent 与 HDC 已获用户确认；本轮新增采集仍需在目标手机完成端到端验收，构建和模拟测试不代替真机测量。
