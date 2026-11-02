# Heterogeneous Enablement Workbench

单用户 Phase-A 异构优化工作台。FastAPI 托管结构化 Agent Engine，并通过统一 Agent CLI Adapter 接入 Kimi CLI、OpenCode 和团队自研 Agent；Agent 负责有状态会话、推理和自主工具循环。后端已有模型配置时直接使用，浏览器也可保存个人模型 URL 与 API Key，并通过短期内存租约传给支持覆盖模型的 Agent。FastAPI 同时负责持久化 Run、不可变 artifact、Trace/Source/KB、MCP 工具、Build Worker 与 Device Gateway；Next.js 负责流式页面和受控 `ui.*` 动作。

本仓库现在是可独立克隆的部署单元，不再依赖父目录中的 `docs_ffrt/`、`openharmony/`、`traces/`、`Xicen/` 或 macOS 工具链。

## Windows 桌面 App · 0.1.93

[桌面 App](desktop/README.md) 在 Windows 本机完成负载采样计划、设备选择、Trace/HiLog/PMU 采样和记录管理，同时提供自定义 Code Agent、SSH Terminal 与本地/远程文件工作区。提供安装版和便携 EXE，内置 Python、PMU 采样器与受管校准微基准；本机设备可直接执行采样，标准归档可继续进入负载特征、校准和并行建模。

桌面 App 还提供 [DPVS / CVS 线性扩展性能分析台](docs/dpvs_cvs_performance_analysis.md)：在中心区域导入单节点 Guest、16 节点 Guest 与 Host 证据，确定性比较 perf、sar 和 NUMA 数据，生成可追溯的 HTML/Markdown 报告，并把有界结构化证据交给右侧 Code Agent 深入归因。导入和报告在本地工作，不依赖 SSH 会话。

0.1.93 修复真机 TraceFS 已显示空闲、但 HiTrace 服务仍停在 `RECORDING_LONG_BEGIN`，导致下一次 `--trace_begin` 返回 `OpenRecording errorCode(1103)` 的问题。Runner 1.2.15 只在用户明确选择的本机直连私有设备上，于取得独占采集会话且确认所有可读 TraceFS 根均为 `tracing_on=0` 后，有界执行 `hitrace --trace_finish_nodump`，复核服务复位且 TraceFS 仍空闲后才开始采样；Bridge 和共享设备不会执行该复位。Trace 收尾的自然等待按缓冲区从 3 秒起分级增长，1 GiB 上限对应 18 秒并保留 25 秒硬上界，避免小缓冲区的异常 finisher 固定占用 25 秒。PMU 二进制身份已确认且兼容路径已接受、但前置 Trace 或操作在 sampler 启动前失败时，诊断改为 `PMU_NOT_STARTED`，不再误报采样器缺失。

0.1.92 将 Code Agent 的输入后提交延迟开放为 `100–5000 ms`，保留 1000 ms 默认值；显式设置 100 ms 时，短消息会在约 100 ms 后真实写入提交键。“失败自动重试”前移到连接配置第二项，负载采样执行区直接显示总开关与手机授权状态。Runner 1.2.14 修复重启后旧采集器无法收尾以及设备脚本退出时的无界等待：只对精确归属的旧进程执行有界 TERM/KILL/reap，默认步骤的宿主清理余量由 180 秒降为 45 秒，并在失败记录中保留 Trace、PMU 或 HiLog 的最后收尾阶段。

0.1.91 为并行建模增加一键 HTML 报告：从已保存的搜索记录沿冻结 ID 汇总负载场景、阶段特征、硬件画像、校准方案、拟合证据、优化公式及最优配置，并附带按历史方案由当前部署资产重建的校准代码快照。报告是无外链、无脚本的离线单文件；桌面版通过原生“另存为”安全写入。并行寻优公式同步改为全宽审阅卡，分开展示目标、约束、字典序优先级、两级求解和符号定义。

0.1.90 在“连接配置 → 自动化与授权”增加可选的错误自动恢复。接入恢复合同的步骤失败后，工作台可把脱敏错误、页面和操作上下文提交给 Code Agent；只有 Agent 通过类型化完成回执确认修复后，才按冻结的设备、计划或服务器目标重试原失败步骤一次。自动恢复、全部手机操作授权和全部服务器操作授权分别保存且默认关闭；授权只约束本次无人值守恢复经工作台工具执行的动作，不改变用户主动点击操作的既有语义。

0.1.89 简化负载采样页“2. 负载与模板”的首屏：负载模板和计划名称保持可见，目标应用包名、Ability、步骤列表、界面准备、执行范围及高级模板参数默认收进“模板详情”。执行主按钮始终可见；字段校验失败或 Agent 返回 blocker 时自动展开并聚焦对应字段；切换模板和历史计划时保留用户当前的展开状态。

0.1.88 为所有桌面错误通知增加一键发送给助手，自动携带报错页面、操作位置、错误码、选中设备和脱敏诊断；异步采样最终失败也进入同一入口。Desktop 对用户明确选中的本机直连私有设备，在新采样前优先严格恢复平台 Trace，归属标记缺失但 `tracing_on=1` 时通过 `hitrace --trace_finish` 保存残留并复核空闲；Bridge/共享设备继续保留未知 Trace。应用异常退出后的回收权限只接受本次启动确认的、目录/设备/项目/计划/run 元数据完全一致的中断记录。

0.1.87 将 Code Agent 初始化收敛为一次长期规则写入，不再串行执行环境、Skill、工具合同和运行状态检查；真实任务再按需发现。Agent 专用 Workspace 通道在连接后后台预热，运行中的会话记录改为 single-flight 异步合并落盘，避免 Windows 慢盘或安全软件阻塞 Electron 主线程与本地工具 Broker。经验演进页将 Agent JSONL 观察跨度与平台墙钟、远端执行、排队、通道获取、RPC 往返和通道启动分开展示，避免把多次累计或端到端等待误判为单次文件读取耗时。

0.1.86 将 Code Agent 工具发现改为“摘要目录 → 单工具完整合同 → 调用”：MCP 和 CLI 默认只提供工具名、能力分组与一句摘要，需要时由 `desktop_tool_describe` / `--describe-tool` 读取一个完整 Schema。经验演进页新增本机 `.cac/projects` JSONL Session 可视化，关联消息血缘、工具结果、耗时、错误以及平台 Skill/工具。采样恢复以终态任务、持久 claim、设备 boot ID、会话 token 和持续 marker 联合确认平台 Trace；回传、归档与计划包使用唯一临时文件和哈希幂等发布，避免重复操作引起文件名冲突，同时继续保留来源不明的外部 Trace。

0.1.85 将“让助手生成计划”改为确定性快路径与 Agent 差量路径：完整模板直接确认可执行；确有缺失项或补充要求时，页面只发送有界的步骤、动作合同和 blocker，Agent 通过一次带 revision/request ID 的 `capture_update_plan` 提交变化。平台保留采集器、PMU 与步骤选择，以原子 CAS、请求幂等和任务级工具权限拒绝并发或迟到覆盖；90 秒提示慢处理，120 秒恢复重试。每轮不再读取全量工具目录、catalog 或重写完整计划。

0.1.84 在“连接配置 → Code Agent”增加独立的“原生 MCP 配置”卡片，可预览并一键复制自配置任务。提示要求 Agent 识别自身真实 MCP Loader、读取工作台 descriptor、保留动态 Broker 环境、完成配置或给出准确启动参数，并只用原生 `remote_status` 与 `capture_catalog` 验证；它不会自动发送，也不会混入长期记忆。

0.1.83 去掉活动栏顶部的 4px 内边距，使左侧活动栏、导航内容与顶部标题栏边界直接衔接。布局回归同时核验标题栏、活动栏、导航列表和首个导航项的纵向起点，避免视觉缝隙再次出现；底部留白、悬浮展开和三种主题合同保持不变。

0.1.82 在 Code Agent 顶部设置按钮左侧增加显式初始化入口。用户点击后，工作台会真实提交一条初始化任务：Agent 读取托管的 `heterogeneous-workbench-tools` Skill，发现 MCP 配置、CLI 回退入口和 Skill 根目录，以只读 `remote_status`、`capture_catalog` 验证能力，再把稳定规则写入自己的长期记忆。CLI 提供 `--list-tools` 自描述目录，返回当前版本真实的工具名、说明和参数 Schema；`capture_save_plan` 等名称不再被误当作系统命令。长提示词提交按本次 PTY 输出序号等待 TUI 粘贴收敛；无回显时按字节数保守等待，正文和单次 CR/LF 分开发送，页面预设任务不会再只停留在输入框中。Agent 启动仍保持零自动输入。

0.1.81 取消 Code Agent 启动时的自动上下文注入，启动和重启都不会向交互式 PTY 写入隐藏文本，首个用户任务保持原文发送。连接配置提供可预览、可复制的长期记忆系统提示，由用户交给自己的 Agent 保存一次；提示只记录稳定的工具使用规则，服务器、目录、设备和凭据不进入长期记忆。0.1.80 的活动栏宽度与图文间距优化继续保留。

0.1.79 将固定 50px 活动栏升级为不占布局宽度的悬浮导航：鼠标进入活动栏任意位置后，一次显示全部功能名称，名称与原图标共用同一导航按钮，点击后立即收起且不会改变页面、Agent 或终端尺寸。连接配置新增“界面”页，可关闭整列展开并恢复单图标提示；偏好保存在当前电脑，深色、浅色与混合主题均使用对应悬浮面板配色。

0.1.78 在并行建模页直接展示与求解器一致的字典序优化模型，包括最低时延、预算内最低频点、稳健性约束及“核心组合内选优—全局选优”两级求解语义。Code Agent 启动时预加载精简的本地目录、SSH 身份/根目录和 `desktop-ssh` 工具说明，通过每会话 readiness token 确认加载完成；超时会自动清理并降级，不阻塞用户首条任务，菜单式 CLI 可关闭该功能。资源管理器新增本地/远程双工作区标签和树上方 Search/Replace；本地路径使用 Main Process grant，搜索有取消和数量/时间上限，批量替换以搜索 revision 重校验并逐文件 CAS 保存。

0.1.77 将真实 Trace 中每个阶段的逻辑 CPU 驻留映射到数据集绑定硬件画像的核组，并用观测/期望 on-CPU 时长闭合驻留证据：覆盖完整才展示主驻留并允许作为源锚，部分或不足时明确降级；没有画像时只报告 CPU 编号，不猜测大中小核。负载特征页新增默认折叠的分型判据与微基准方法说明。受管微基准在目标线程正式计时窗内用 `perf_event_open` 同步采集 cycles、instructions 与通用 cache reference/miss，并按 enabled/running 缩放；频响曲线公开分段公式。结构与来源完整性仍阻断发布，统计质量或覆盖门未通过但已有可用实测和估计参数时发布显式标险的 `risk_calibrated`，可用于寻优但持续显示证据等级和失败门禁。活动栏点击后立即收起 hover tip；采样设备与硬件画像按稳定设备身份显式关联，不按 CPU 编号或列表首项猜测。

0.1.76 为频点曲线补充可审计的源码强度与源码吞吐证据，并新增显式可选的五档 Classic Roofline 真机扫描；0.1.75 的频率恢复状态机及物理核/逻辑 CPU 拓扑合同继续保留。

本轮同步梳理现有平台、Bridge 故障根因和桌面迁移路线，入口为 [桌面工作台设计与平台架构复盘](docs/desktop_workbench_design.md)。0.1.26 的连接原型已获用户 Windows 实测确认，0.1.27 起已接入本地采集；现有 Bridge 1.4.9 继续作为 Web 兼容链路保留。

0.1.28 修复 Windows 保存计划时目录同步触发的 `Permission denied`。频繁更新可在 Windows 仓库根目录执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update`：拉取代码、本机构建并启动，依赖和 Python 缓存复用，原有 AppData 配置与 Trace 保留，无需每次传完整 EXE。首次准备和更新说明见[源码运行](desktop/README.md#从源码运行与构建)。

0.1.29 将助手固定在右侧、SSH 终端置于下方（默认约四分之一高度），导航/助手宽度和终端高度均可拖拽调整并自动保存。面板跨页面保留，可收起和重置；提交采集计划任务时保留当前采集页面，直接在右侧查看 Agent。

0.1.30 修复全新源码运行的 `electron/path.txt` 缺失：在 npm 安装后显式准备并验证 Electron 本体。只缺运行时文件时单独修复，下载失败后重试保留 npm 包；现有 0.1.29 EXE 已含 Electron，不受此源码启动问题影响。

0.1.31 继续修复 Electron 下载时的 `fetch failed`：自动启用环境代理，默认源网络失败时有限重试并回退国内镜像，支持指定本地官方 ZIP；始终核对锁定 SHA-256，并显示底层网络或解压错误。沿用上面的源码更新命令，[网络受限及离线导入步骤](desktop/README.md#源码准备下载失败先看失败阶段)已补入使用说明。

0.1.32 让 Electron 准备继承 npm lifecycle 中的代理、企业 CA 和专用镜像配置，并在启动下载进程前清除 registry token 等无关字段。npm registry 只提供 npm 包，不能代替 Electron ZIP 镜像；公司镜像可通过 `HETERO_ELECTRON_MIRROR` 显式接入。

0.1.33 为后续 CPython 运行时增加同等级的离线与企业网络支持。`HETERO_PYTHON_ARCHIVE` 接受未解压的官方 Windows embeddable x64 ZIP，固定哈希通过后零网络构建；`HETERO_PYTHON_MIRROR` 接入企业镜像。Python 准备也继承 npm 代理与 CA，但不会把 npm registry 猜作 Python 文件仓库。Electron 和 Python 分别设置 archive，步骤见[源码准备下载排障](desktop/README.md#源码准备下载失败先看失败阶段)。

0.1.34 允许桌面采集的**源码运行**显式复用电脑上已有的 CPython 3.11–3.15。设置 `HETERO_DESKTOP_PYTHON` 后，启动器以 `-B -I -S -X utf8` 隔离校验解释器、所需标准库和真实采集闭包；全部通过才跳过内置 Python 下载，配置无效会直接失败而不会静默改用网络。Windows 已安装 Python 3.15.2 时可执行：

```powershell
$env:HETERO_DESKTOP_PYTHON = ((& py -3.15 -B -I -S -c "import json, sys; print(json.dumps(sys.executable, ensure_ascii=True))") | ConvertFrom-Json)
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update
```

`-Package` 和发布 EXE 始终使用固定内置 CPython 3.13.17，保持自包含。该合同仅覆盖桌面采集源码运行，不表示 FastAPI Web 后端及其第三方依赖已经支持 Python 3.15；完整边界见[桌面源码运行说明](desktop/README.md#使用电脑上已安装的-python)。

0.1.35 将桌面骨架与 Web 工作台的顶栏、侧栏、标题、卡片和状态色统一，并补齐 xterm 的 Windows 常用剪贴板行为：有选区时 `Ctrl+C` 复制，无选区时 `Ctrl+C` 中断命令，`Ctrl+V` 粘贴；macOS 对应 `⌘C/⌘V`。布局存储升级为 v2，使旧安装首次启动即采用新默认尺寸。

0.1.36 将 SSH Terminal 改为工作区底部通栏，同时横跨功能页面和 Code Agent；两块上方面板继续保持可调宽度。连接工作台新增限定在 SSH 工作目录内的远端文件树和 UTF-8 文本编辑器：文件读取与保存上限为 1 MiB，保留 BOM 和统一换行格式，通过内容版本双重复核与原子替换降低并发覆盖风险，并拒绝越界路径及指向工作目录外的符号链接。场景采集页按 Web 平台的信息架构整理为“场景与步骤、采集器与设备、执行动作、已保存计划、采集结果”，本机 HDC 执行链路不变。

0.1.37 将桌面壳层改为 VS Code 风格的深色工作台：Code Agent 在右侧全高贯穿，SSH Terminal 回到内容区底部并可在面板内折叠；固定 50px 活动栏只显示图标，悬停或键盘聚焦时显示页面名称。资源管理器图标进入远程文件工作区；0.1.72 起文件树作为该工作区的固定结构，不再由活动图标切换折叠。文件仍通过当前 SSH 会话读取、编辑和原子保存。SSH 密码按服务器、端口和用户名使用 Electron `safeStorage` 绑定本机系统账户加密保存，后续密码框留空即可复用，普通设置和页面状态均不包含密码或密文。

0.1.38 增加全局工作台缩放：焦点位于页面输入框、远程编辑器或终端时，`Ctrl++` / `Ctrl+=` 均可放大，`Ctrl+-` 缩小，`Ctrl+0` 恢复 100%；macOS 同时支持 `⌘` 组合键。缩放作用于完整工作台并按当前系统用户持久化，重新启动后沿用上次比例。

0.1.39 修复远程文件读取结束时 SFTP 文件句柄与通道关闭的竞态：文件按 64 KiB 分块读取，并在远端 `CLOSE` 确认后才结束通道，避免慢服务器触发 `No response from server` 主进程弹窗。Code Agent 默认宽度提高到窗口的约 40%，最大可拖到 900px。

0.1.40 增加“深色 / 浅色 / 白 + 黑”三种工作台主题，当前统一从 Electron 原生“主题”菜单选择。浅色主题覆盖页面、资源管理器、Code Agent 与 SSH Terminal；“白 + 黑”让中央页面和 SSH Terminal 保持白色，同时保留右侧 Code Agent 的深色终端。主题切换不重建终端，选择按本机持久化并在下次启动时恢复。

0.1.41 确立 Desktop-first 执行拓扑并增加“服务器环境”页。Windows Desktop 持有 Code Agent、HDC、采集、原始 Trace 与真机执行；Linux 收敛为源码分析、Trace 解析和异构测试案例编译 Worker。用户只配置远端工作仓、产品源码、OpenHarmony 头文件能力面、LLVM、glslang、TraceStreamer 和 AArch64 运行库；state、cache、assets、tmp、logs 与 run 由平台按 profile 隔离创建。页面可执行只读预检、托管目录初始化和 Agent 补齐，详细合同见[设计文档第 23 节](docs/desktop_workbench_design.md#23-desktop-first-服务器-worker-合同0141)。

0.1.42 将页面内的第二顶栏收回 Electron 原生菜单：主题、布局和连接配置与“编辑”“视图”位于同一菜单行，布局菜单统一控制助手、终端和布局复位。Code Agent 保留真实 CLI PTY，移除重复的页面内输入框；SSH 面板移除状态页脚与单独的断开按钮，并在同一 SSH 连接上提供最多 8 个、各有独立 `terminalId` 的交互终端标签。固定只读的“Agent 执行”标签展示 Agent `remote_exec` 的命令与结果，但不向任一交互 Shell 注入字符；浅色主题同时收敛 ANSI 色板，避免终端文字残留白色。0.1.70 进一步移除当时保留的面板连接按钮，改由保存配置和新建终端触发连接。

0.1.43 打通本机采集到负载特征的完整链路。首次进入分析页时，Desktop 会为当前 SSH 目标自动初始化尚未创建的服务器 Profile 并重试；场景采集和 Trace 管理中的标准 `capture.zip` 可直接整包同步，服务端一次识别 Trace、PMU、HiLog 与元数据，按采集步骤创建数据集并立即提取负载特征。Web 的采集包导入也使用相同的整包数据集构建逻辑，不再要求分别导入 Trace 和 PMU 文件。

0.1.44 修复 Linux 分析 Worker 使用 Python 3.10 时，打开负载特征页因 `datetime.UTC` 而在 `runpy.py` 中止的问题。远程 Desktop RPC 与服务器 Profile 预检现统一支持 Python 3.10+，运行库哈希校验也不再依赖 Python 3.11 的 `hashlib.file_digest`；Windows 本机采集运行时仍沿用独立的 CPython 3.11–3.15 合同。

0.1.45 将桌面窗口收敛为一条 VS Code 风格标题栏：移除产品名和版本文字，应用图标位于“编辑”右侧，“编辑 / 视图 / 主题 / 布局 / 连接配置”随工作台缩放；Windows 右侧继续使用原生最小化、最大化和关闭按钮，保留系统贴靠与窗口行为。

0.1.46 增强远程分析环境的自恢复能力：Desktop 会从服务器 Profile、当前 SSH 工作目录及 Git 根目录发现分析 Worker，并依次验证已配置 Python 与仓库已有虚拟环境；检测到过期的 `platform.env` 后按当前服务器配置重建并自动重试原请求。路径与既有运行时无法安全恢复时，错误区可将脱敏后的环境、请求和错误上下文一键提交给右侧 Code Agent。平台只自动执行环境发现、托管配置重建和同请求重试，不会盲目执行 `pip install`、`git pull`、`sudo` 或修改系统 Python。

0.1.47 调整桌面标题栏与资源管理器细节：应用图标移到“编辑”左侧；原生下拉菜单改用窗口内容区相对坐标定位，并同时处理页面缩放、多屏坐标和窗口边缘回退；文件树目录展开符改为 CSS chevron，统一图形尺寸并做光学居中。

0.1.48 让远端资源管理器在 SSH 连接后先读取根目录，再以单个 SFTP 通道有界预取子目录；常见依赖、构建和缓存目录及符号链接不递归扫描，未预取目录继续按需读取。目录树每 10 分钟在上一轮完成后刷新，保留展开、焦点、活动文件和上一次成功结果，目标切换会丢弃旧请求。Desktop 同时随版本携带带 SHA-256 manifest 的最小分析 Worker；服务器缺少兼容 Worker 时，平台将其原子部署到当前 target/profile 的 `assets` 并自动重试，不修改用户仓库，也不执行 `git pull`、`pip install` 或 `sudo`。部署握手使用的内部 marker 只供程序判定，不显示给用户。

0.1.49 将分析算法与 FastAPI Web 路由依赖解耦。内置 Worker 现在可在禁用 Python site-packages 的环境中执行负载特征、并行建模和寻优校准，不再要求 Linux 服务器安装 FastAPI、Starlette 或 Pydantic；Web 服务仍在创建 HTTP 路由时按需加载原有框架。

0.1.50 修复“带入并行建模”在 Desktop 常驻页面中重复选择同一负载时不重新生效的问题。每次带入现在以独立 revision 原子应用数据集和特征版本；并行建模首个入口改为“负载归档”，直接列出当前服务器 Profile 中由设备采集、Trace/解析结果导入及历史样例形成的全部可分析负载，并避免重复展示已经物化的内置样例。

0.1.51 增加硬件模型管理闭环：Windows 可对用户选中的本地 HDC 设备只读提取 CPU 核组、逻辑 CPU、频率域与 OPP，保存为版本化硬件配置；负载通过派生新版本绑定配置，并行寻优只选择与该配置匹配的代价模型。尚未实测校准的新设备使用明确标注的拓扑迁移模型，结果持续标为条件预测。场景采集页同步收紧步骤列表，并将采集模式、设备连接和 PMU 参数重排为规整布局。

0.1.52 将产品入口统一为“负载采样”，连接配置同时固定在左侧活动栏底部。各工作页压缩为一个可解释的短标题，说明移入标题悬停提示；每个步骤只保留一个贯穿内容宽度的主操作，次要配置和诊断保持就近可用。长任务在原步骤内持续展示进度，完成后平滑聚焦下一步主操作；该按钮获得键盘焦点后可直接按 Enter 执行。默认值只在选择唯一且不会误绑设备、硬件或模型时自动采用，多候选继续要求用户明确选择。界面只使用“负载采样”“远程采集端”等任务术语，Bridge、内部 page ID 和兼容协议名保留在实现与运维文档中。

0.1.53 修复精简鸿蒙 shell 缺少 `tr` 时硬件提取错误返回零频率域的问题，并取消“三频率域、八物理核”的固定假设。探测保留 1 至 64 个真实 cpufreq 域，按显式代理核型迁移未校准系数；硬件模型编辑、保存和并行搜索均使用动态核组。该版本曾将 VYG-AL00 的 12 个逻辑 CPU 显示为物理核；0.1.75 根据兄弟拓扑更正为 8 个物理核 / 12 个逻辑 CPU，三个频率域为 4/6/2 个逻辑 CPU。负载采样同步改为“设备连接—负载与步骤—采样配置”，采集模式无外框，PMU 主参数同排；历史计划下拉框与执行按钮相邻，选择后可直接执行，编辑后保存形成新版本。

0.1.54 将负载特征入口收敛为一个“选择负载数据集”：本机采样步骤、尚未物化的内置历史归档和既有归档数据集在同一选择器中分组展示；采样步骤使用“`[计划名称] Step N: 步骤名称`”标识来源。用户只需执行“提取负载特征”，平台按来源完成导入或物化并生成特征版本。逐阶段表格位于 BuildItem 时间位置图之前，点击时间条后，所选 BuildItem 的调度拆解和 PMU 详情紧邻显示。Desktop 活动栏同步采用蓝色图标、加粗蓝色指示条和页面底色，清晰标识当前功能页。

0.1.55 将 Web 与 Desktop 的计划动作收敛到同一行：依次为保持固有宽度的“保存计划”、约占三分之一可用宽度的历史计划选择器，以及占据其余空间的“在设备执行采样”。页面不再单独显示“已保存计划”标题；采样开始后，执行按钮原位切换为“取消采集”或取消中的状态，不再在“保存计划”旁增加独立取消按钮。

0.1.56 统一主操作与硬件模型卡片的视觉规格：“让助手生成计划”复用“在设备执行采样”的主按钮样式，并在深色、浅色和“白 + 黑”三种主题下保持悬停文字清晰；硬件模型提取行的设备选择、读取设备与提取配置统一为 38 px 高度，核组卡片和已登记硬件模型卡片分别在同一行内等高对齐。

0.1.57 修复 Desktop 并行搜索被 120 秒执行上限和服务器页面刷新风暴中断的问题：分析请求最多占用两条 SSH 命令通道，其余排队；一次搜索始终使用一条远程命令，并获得独立的 10 分钟执行窗口。服务器在同一个 Python 请求内以最多 8 个受控进程批量仿真配置，3360 项实测由 39.9 秒降至 7.6 秒；最短时延默认只枚举最高频点，预算内最低频点自动恢复全 OPP 搜索。并行建模在拆解账目时自动生成所选硬件的不可变负载版本，不再要求预先点击“生成新硬件版本”；连接配置抽屉也完整避开顶部窗口控制栏。

0.1.58 统一 Web 与 Desktop 的负载采样默认行为：首次打开和重新进入页面会自动刷新设备，重复的 HDC 发现请求合并执行，后台刷新保留用户明确选择的手机；存在历史计划时默认选中最新计划，保存或由 Agent 生成的新计划立即成为当前计划。采集模式与 PMU 主参数合并为一条配置带，“保存计划”和历史计划选择器使用等宽列，执行操作继续占据双倍宽度。

0.1.59 将“保存计划”缩减为原宽度的一半，并把空间交给历史计划选择器；并行建模的搜索记录改为紧凑的“目标—时间—预测时延”标签。工作台启动后自动恢复已配置的 SSH 与 Code Agent，缺少配置时直接定位到相应配置页。应用流程、机会推理、代码仓、代码知识库、优化知识库、技能列表和经验演进已迁入 Desktop，并通过版本化分析 Worker 读写与 Web 相同的服务器状态。

0.1.60 增加 SSH Terminal 横向拆分：右键终端标签即可同时显示两个真实 Shell，并拖动分隔线调整比例。远端编辑器改为多文件标签，每个文件独立保留内容、revision、未保存状态和光标位置。优化知识库新增 NPU API、GPU API 与自建 API，用户可指定服务器源码目录，平台复用既有 API 知识库 Skill 提取接口合同。代码知识库首次遇到缺失的 Tree-sitter C/C++ 解析器时，会在当前 SSH target/profile 的独立托管环境中安装锁定版本并重试，不修改系统 Python 或用户代码仓。

0.1.61 将 Desktop 机会推理恢复为 Web 已形成的四步方法：自动识别当前本机 HDC 设备与操作系统画像，选择真实 Trace 并提取热点，将热点关联到代码仓中的可核验源码，再复用 Web 的硬件中立代码画像与能力卡构造模型路由上下文。Raw HTrace 使用 Web 同源的父子调用栈扣减、HiTrace Tag 解码和模块分组；界面区分插桩 Span 自耗时估计与降级的 Trace Span 指标，后者不会被误称为 CPU self-time。平台不按关键词打分或预选硬件；右侧 Code Agent 先仅依据隔离的模型上下文选择硬件单元，再读取 Trace/源码审计证据与 API 目录开展机会推理。应用流程与机会推理的 Trace 选择器同时列出服务器已物化 Trace 与本机 Trace 管理中的可分析步骤，本机记录选择后按需物化到当前服务器 Profile。标题栏菜单悬停 140 ms 即打开；资源管理器在一条 SSH 连接内长期保留交互与后台预取两条 SFTP 通道，通道超时、关闭或协议错误时仅淘汰故障通道，并由下一次操作自动重建。

0.1.62 的寻优校准支持在 Desktop 导入最大 20 MiB 的微基准实测 JSON：文件经一次性预约、target/profile 围栏、SHA-256 和 typed SFTP 暂存合同送入与 Web 相同的拟合引擎，完成或失败后清理暂存。Trace 的 `observed wall = observed On-CPU + observed Off-CPU` 是观测账目恒等式；预测只缩放 On-CPU，保留非 Binder delay 节点，并以 send → service → reply → wake 依赖链表达同步 Binder 等待，整段时延取 DAG makespan。`app_off_cpu_observed_ms` 只报告观测账目，不能直接加到预测 On-CPU。compute、memory、mixed 代理按核组覆盖至少 6 个实测 OPP，其中至少 5 个参与拟合、1 个完全留出，硬件 min/max 必须进入训练集；分段曲线仅在训练频率闭区间内使用，域外显式回退旧弹性。PMU 候选分型只负责选择已实测曲线，两种频响不会重复相乘。详细合同见[寻优校准说明](docs/trace_storage_and_calibration.md)与[条件并行模型](docs/parallel_modeling_model_v1.md)。

0.1.63 将服务器调用从“每次启动 Python”改为持久 Platform RPC：同一 SSH target/profile 下，页面请求与 Code Agent 工具使用彼此独立的 `page`、`agent` 长驻 Python channel，分别复用解释器与模块导入；目标或运行环境切换会关闭相关通道，单请求取消、超时和协议故障只淘汰其所属通道，后续均按需重建，仍不把它描述为可恢复后台任务。本地 Agent 只暴露工具合同已经完整迁移的 `application-execution-flow-reconstruction` Skill，启动时按精确文件清单、大小和 SHA-256 复核只读副本。Desktop 的 `agent-runs/` 保存任务摘要、经控制字符清理的有界 PTY 可见输出片段，以及经 Agent broker 发生的 Platform RPC/SSH 工具耗时；Bearer、密码、token、API key 和私钥块在写盘前脱敏。它不保存完整对话或模型内部过程，无法归因的 PTY 静默只标记为未归因等待。local-first 设计把状态分为 Global Host、Workspace、Window Instance；当前版本仍是单窗口，且应用流程、机会推理、代码知识、Skill/Experience 等迁移页面的多数领域状态仍在远端 `HETERO_STATE_ROOT`。

0.1.64 澄清并优化分析页首次读取。页面显示的“读取模型”实际读取负载归档与特征版本、硬件画像与兼容代价模型、校准方案与历史拟合、既有分析与寻优记录，不会下载大模型权重，也不会启动模型推理。长驻 stdio Worker 按状态根复用 `ParallelModelingManager`，避免每个目录请求重复校验发布版建模 bundle；页面请求与同步 Worker 对齐为单并发，尚未执行的当前活动页请求优先于已隐藏页面，同优先级仍按进入顺序执行。负载特征、硬件模型、寻优校准和并行建模页的加载文案直接说明正在读取的数据。该版本尚未合并页面 bootstrap 请求，也未建立历史记录摘要索引；大体量历史目录仍按现有领域合同读取。

0.1.65 把寻优校准改为自动真机实验。平台从本机 HDC 设备提取动态硬件画像，生成绑定核组、OPP 与物理核的冻结计划，部署内容寻址的 AArch64 OpenHarmony 微基准，自动完成锁频、绑核、计算/带宽屋顶、核频响应、核组倍率、并发争用和 worker 投递测量，再执行拟合、留出验证与模型发布。页面增加可调频率的经典 Roofline 图，并把它与用于时延寻优的分段 `G(f)` 曲线分开解释。Instruction Roofline 仍要求 retired instructions 与 DRAM bytes 同窗 PMU 对齐；在该证据完成前不会把整数代理写成硬件指令屋顶。

0.1.66 将硬件模型选择放到校准主路径。选择器列出“硬件模型”页中全部未归档配置，并标明平台内置、本机 HDC 提取或用户添加；多模型时必须明确选择，模型库新增、归档或恢复后页面自动刷新。点击“一键生成微基准并校准”后，平台直接用所选模型生成冻结计划，不再隐式提取并注册重复画像；Desktop 在真机执行前重新读取当前设备，只用于严格核对核组、物理核和 OPP。

0.1.67 将经典 Roofline 改为双变量交互图。频率滑块上下移动当前计算屋顶，计算强度滑块在对数横轴上移动“负载”点；纵坐标始终由 `min(Ppeak, BW×I)` 计算。图中删除独立拐点标签与固定强度长标签，只保留“负载”；当前屋顶接近 50%/100% 参考线时自动隐藏冲突标签，负载标签在边缘自动翻转。理论边界超出图示 OPP 范围时直接说明整个频段的受限状态。

0.1.68 把 Roofline 频率滑块下限和最低参考线统一调整为 20% OPP。当前计算屋顶与带宽斜线的交点向横轴投影为动态竖线；竖线左侧带宽斜线以下区域标为 `Memory-bound`，右侧计算屋顶以下区域标为 `Compute-bound`。两区采用轻量底色，深浅主题分别适配；负载标签在低频、低计算强度下会自动避让区域标题。

0.1.69 将校准 `prebuilt` 资源声明为逐字节文件，避免 Windows Git 把构建回执的 LF 改写为 CRLF。为兼容已经受影响的工作区，运行时只在 `CRLF → LF` 后精确命中清单哈希时接受并重新打包为规范 LF；ELF 二进制始终按原字节验证，清单、回执哈希、二进制哈希与 AArch64 ELF 身份分别给出明确错误。Desktop 同时增加窗口级错误通知中心：错误固定在当前视口顶部且不阻塞操作，正文和复制图标均可复制完整信息，关闭按钮只关闭对应错误；主要业务页、原生采样、服务器配置与远程文件错误使用同一行为。

0.1.70 删除 SSH 面板的“连接 / 重新连接 / 切换连接”按钮以及 Code Agent 面板的“启动 / 停止”按钮。保存 SSH 配置后，目标或认证信息发生变化会自动切换连接；SSH 离线时点击“新建终端”会先按已保存配置连接，再创建并复用主终端。保存 Code Agent 配置会在未运行时自动启动，在运行配置变化时安全重启；唯一的手动“启动 / 重新启动 Agent”入口位于“连接配置 → Code Agent”页，并紧邻“保存配置”。

0.1.71 增加统一的 Remote Workspace Transport。资源管理器、后台预取、Code Agent 的结构化文件工具和轻量远端命令复用已经认证的 SSH 连接，并按 `interactive`、`prefetch`、`agent`、`platform` lane 隔离队列与故障；远端标准库 Worker 通过 SSH channel 的 stdin 启动，不要求服务器开放端口。文件保存继续使用 revision/CAS、同目录临时文件和原子替换；读取与编辑保留 UTF-8 BOM、LF/CRLF/CR 和 SSH 目标切换保护。

0.1.72 将资源管理器改为独立服务器工作区：左侧远端文件树与右侧多标签 Monaco 编辑器之间使用可拖动、可键盘调整的分隔条；再次点击已经激活的资源管理器图标保持当前工作区，不再折叠文件树。Monaco 0.53 及其 Worker 随 Desktop 离线打包，遵守本地 CSP，提供按扩展名语言高亮、查找、多光标、每文件撤销/视图状态、三态主题与 `Ctrl+S` 保存。连接测试客户端移到“工具集成”，页面不再混入连接概览和远程执行记录；Markdown/Mermaid 预览保留为复用现有安全渲染组件的扩展点。

0.1.73 重构一键校准参数体系。默认方案按每个核组、compute/memory/mixed 类型和 OPP 实测单位工作量时延，并用目标/参考绝对曲线成本比值完成换核与换频预测；有向负载组合干扰矩阵成为默认并发模型。Worker 开销改为同一持久 Worker、CPU、OPP 和 payload 下的 direct-batch 与逐项 queue-dispatch 配对差分，分别保存端到端 wall、Worker CPU 和 producer CPU；线程池冷启动独立于每任务暖态派发。受管 AArch64 微基准升级为 `parallel-calibration-proxy-v4`。

0.1.74 在完整 PMU 事件不足时，仍使用已有 IPC、Cache/LLC MPKI 或有效非零基础计数生成带置信度与缺失证据的 Running 负载候选；service/scheduler 继续作为阶段主特征。通过质量门禁的真机代理微基准模型可作为离线包随分析 Worker 幂等安装，校准页底部按实际核组 × 任务类型展示各 OPP 实测中位数、拟合线和适用域。远程工作区增加有界 Markdown/Mermaid 安全预览，以及重命名、复制路径、文件复制/粘贴、分块下载和预览。

0.1.75 修复真机实测达到总数后停在 `cleaning` 的假失败：页面会持续跟踪到原始 cpufreq 边界恢复完成，在设备仍在线时有界合并重试；只有频率恢复失败才阻止拟合，临时二进制删除失败只保留为可见警告。硬件提取器优先使用完整互证的 `core_cpus_list` / `thread_siblings_list`，硬件模型保存每个物理核到逻辑 CPU 的映射，页面同时显示两种数量。受管微基准使用同一映射验证目标核、helper 和 observer，并升级为 v6：在目标窗口内核验同物理核其余逻辑线程的 `/proc/stat` 忙占比；频率观测绑定目标/helper 各自的 per-CPU `cpuinfo_cur_freq`，并用最多 12 轮同负载预热等待连续 3 次命中目标 OPP ±3%。policy `scaling_cur_freq` fallback 只供诊断。频率或同胞线程质量门失败时，同一个 repeat 最多补测两次并保留全部尝试证据；v4/v5 数据不再进入生产拟合。

真机推理统一支持三类输入。当前“应用流程重建结果”入口只列出已经完成真实 Trace 校准的场景，并提供显式流程阶段选择；平台把所选阶段映射到运行时 stage ID，并只展示属于该阶段的热点。未校准流程仍可用于流程理解，但不能从该入口进入热点推理。独立真实 Trace 提供运行指标、事件与源码匹配；用户粘贴热点代码提供显式代码并标记为未绑定。三类输入都进入同一硬件中立路由；降级 Trace Span 不是 CPU self-time，粘贴代码不携带虚构的 revision、频度或性能占比。

## Repository Boundary

| 内容 | 位置 | 是否随 Git 部署 |
| --- | --- | --- |
| Web、API、Agent Runtime、Build Worker、Windows Bridge | `app/`、`backend/`、`device-bridge/` | 是 |
| 优化、硬件、API 与最小规则知识库 | `data/knowledge-bases/` | 是 |
| Skill 泛化评测集 | `data/evaluation/` | 是 |
| 页面 Agent 的受审查硬件 API 模板 | `agent-resources/hardware-api-templates/` | 是 |
| AST、源码快照与规则匹配工具 | `tools/analysis/`、`backend/source_revision_snapshot.py` | 是 |
| Spec、总体技术文档和架构图 | `docs/` | 是 |
| 用户源码仓、原始 Trace、LLVM、SDK、TraceStreamer、glslang | 管理员指定的外部路径 | 否 |
| Desktop 设置、采样/Trace、Code Agent 运行证据 | 系统用户数据目录 `%APPDATA%/HeterogeneousWorkbench` | 否 |
| 过渡期远端分析状态、上传、构建缓存和 Web 审计记录 | 服务器 `HETERO_STATE_ROOT` | 否 |

源码仓通过页面添加、Git clone 或 `HETERO_DEFAULT_SOURCE_ROOT` 接入。完整 OpenHarmony checkout 只在需要分析其源码时存在，不是平台启动条件。真机编译只需要由 `provision_openharmony_sdk_bundle` 提取出的紧凑 SDK 头文件包。

## 负载采样、负载特征与并行建模

左侧「负载采样」提供图库 17 步模板，默认选中第 7 步编辑操作；可输入新操作步骤，交给内置 Skill 生成结构化计划与可下载 Python。进入页面后自动汇总本机与在线远程采集端的设备；完整发现仅有一台时自动选中，多台设备或局部探测失败时要求选择目标。选定后，定期与重新聚焦时只刷新该连接路径的状态，「刷新全部设备」重新列出全部候选，采样期间暂停检测。两种连接均按步骤回收 Trace、HiLog、PMU，支持自动准备或从已准备的页面开始采样。本机 HDC 可通过 PATH 自动发现或由 `HETERO_HDC_BIN` 指定。

「让助手生成计划」通过当前 Agent 的命令接口提交完整草稿。后端在粘贴后保守等待并观察终端输出，再发送提交键，适配较慢的内部 CLI；默认只提交一次。升级后重启后端并刷新页面，新终端回执含 `last_submission.strategy_version=pty_output_settle/v1`，可核对实际等待和提交次数。具体协议及旧 Adapter 配置迁移见 [Agent 框架接入指南](docs/agent_framework_adapter_guide.md)。

采集前自动亮屏并尝试滑开无密码锁屏，重新读取布局确认后再开始测量。当前自动模式会关闭图库旧进程、重启并核验主页；“已准备”保留用户选定的起点，非图库应用不执行图库重置。正常准备可取消，每条命令有独立超时和有限尝试，没有累计 45 秒限制。PMU 结果分别展示计数完整性和实际采样质量：线程运行前的零累计基线不算读取失败，采样间隔、跳点和窗口覆盖以实际记录为准。

平台 `0.1.25` / Bridge `1.4.9` 修复“Windows Bridge 在线、已识别手机，但采集设备列表检测超时”的结果回传堵塞：当前任务的接收、进度、结果与历史 spool 补传分别使用独立 HTTP 通道；慢历史回执不再占用新发现结果的发送位置，同一任务内顺序与重复回传保护继续执行。页面区分 Bridge 在线、手机列表读取中、读取超时和确实未发现手机。服务器已为 `0.1.24` 时，此回传修复只需更新 Windows 代码并停止旧进程后重启 Bridge（启动输出 `implementation_version=1.4.9`）；服务器更新到本版本可获得对应状态提示。

平台 `0.1.24` / Bridge `1.4.8` 修复 HTTP 轮询被 HDC 诊断与历史回执回传阻塞的问题：诊断和回执各由独立后台任务处理，轮询持续维持心跳；重连时批量记录历史任务回执，减少全量 registry 读写与全局锁占用。HTTP 超时日志包含请求端点、耗时和时限，便于区分网络轮询、结果回传与设备检查。此版本需要服务器更新重启，同时在 Windows 更新仓库代码，停止旧 Bridge 后重新运行 `deploy/windows/run-device-bridge.ps1`；启动输出应显示 `implementation_version=1.4.8`。

平台 `0.1.23` 恢复场景采集页与页面助手两处设备选择，两处共用同一目标及连接路径；已提交任务继续保留提交时的设备。连接路径选择独立于手机发现结果，可先选择本机或已登记 Windows Bridge，再刷新当前连接。页面刷新后保留选择，发现失败展示 HDC、Bridge、超时等具体诊断，不将检测超时当作设备离线。Windows 本地采集包可在平台未发现手机时下载，Windows 端仅在唯一设备时自动绑定，多设备要求显式指定序列号。服务器更新重启并刷新页面即可，无新增 Bridge 协议。

平台 `0.1.22` 增加 Windows 本地采集闭环。场景采集页可将保存的计划、固定 Runner 与已校验 PMU 采样器打成自包含 ZIP；PowerShell 在连接手机的 Windows 上直接调用本机 HDC，避开 Bridge 轮询、全局 HDC 队列和 `bridge_job_deadline`。结果生成标准 `.capture.zip`，Trace 管理以流式上传、安全解包、文件大小与 SHA-256 校验、原子发布和幂等冲突检查导入服务端；Trace、HiLog、PMU 与校验元数据随后可直接进入负载特征和并行建模。原 Windows 目录继续保留，便于本地工具查看和自行清理。

平台 `0.1.21` 将设备选择绑定为 `connection_mode + bridge_id + device_serial` 完整路径，页面助手在发起任务时保存目标快照，准备、采集、文件回收与重试沿用同一设备。已选目标采用轻量定向发现；其他 Bridge 的离线或探测失败按连接路径分别记录，不阻断有效目标。未选目标时汇总所有路径，部分发现失败保留可选设备并等待明确选择。右侧助手设置展示当前采集设备，执行结果缓存按设备路径隔离。默认 Skill 配置升级为 `agent-centric-page-device-selection-2026-10-26`，场景采集 Skill 为 `1.4.0`，旧版配置保留。更新服务器并重启、刷新页面即可使用，Windows Bridge 协议不变。

平台 `0.1.20` / Runner `1.2.12` 精简默认采集链路：采集会话按剩余租期与下一操作耗时续租；正常页面不做固定亮屏等待；已确认的保亮设置按需刷新；临采前保留一次新鲜布局核验，页面异常才执行唤醒与解锁。电源查询接口在本轮内复用，已明确不支持的命令不再反复尝试。脚本按 UTF-8 实际大小合并上传分片。坐标适配、采集入口归属、取消清理和 Trace/PMU 数据质量校验继续执行，服务器/API 更新重启即可使用，无新增 Bridge 协议。

以下为版本变更记录；当前准备策略以 `0.1.25` 为准，历史版本的累计预算已取消。

平台 `0.1.2` 的锁屏准备增加亮屏等待、按实际尺寸换算手势速度及第二种上滑策略。准备失败会尝试回收手机截图，在采集结果中提供“查看失败现场”及操作诊断，支持无法现场查看设备时远程排障。手机不支持截图或传输失败时仍保留原始错误；历史失败记录不会补造截图。

平台 `0.1.3` 自动维护已选设备的租约，Agent 使用发现结果中的 Bridge/逻辑设备即可调用远程 probe 与后续操作。修复心跳覆盖租约、发送前离线留下占用及快速结果被旧状态覆盖的问题；同一设备已发送且结果未知的任务仍等待对账。服务器更新后重启平台即可使用，Windows Bridge 继续沿用现有协议。

平台 `0.1.4` / Runner `1.2.2` 在确认无密码锁屏后先尝试 Back，再按需上滑；USB 连接方式弹窗自动关闭并重读布局核验。标准命令使用 `uitest uiInput keyEvent Back`，解析不支持时兼容小写形式。弹窗持续存在会保留失败现场；新版 `scenario-step-capture` Skill 解释运行时准备记录。更新服务器并重启平台即可使用，Windows Bridge 协议不变。

平台 `0.1.5` 新开场景采集页时不自动打开历史结果或展示旧失败。仅恢复仍在执行的采集任务；任务完成后自动清除浏览器恢复标记，历史记录仍可手动查看。

平台 `0.1.6` / Runner `1.2.3` 在标准亮屏后查询电源状态；命令失败或设备仍未亮屏时，尝试一次专用 Wakeup 键并再次查询。Back、动态尺寸上滑及新布局核验继续保留，策略版本为 `power_wake_back_swipe_usb/v4`。每步屏幕准备使用共享的 45 秒预算，单次设备 I/O 请求最多 8 秒，并为执行器退出及结果回传预留时间；失败现场的电源/锁屏诊断与截图另用共享的 20 秒取证预算，采集器和设备锁清理另计，因此 45 秒不是整项采集任务的完成时限。任务详情持续显示当前操作及耗时，取证失败保留原始失败原因。

此版本需更新并重启服务器；使用 Windows Bridge 时，还需更新 Windows 上的 Bridge 脚本，停止旧进程并重新运行 `deploy/windows/run-device-bridge.ps1`，核对 `implementation_version=1.4.7`。该版本修复 HDC 排队与文件回收放大短超时的问题，避免过期的设备命令排队后继续执行。只更新服务器不能替换 Windows 上已运行的 Bridge。

平台 `0.1.7` / Runner `1.2.4` 将开屏解锁固化为所有场景共用的执行前准备：先查电源，明确灭屏时按一次 Power，等待 1 秒；识别到无密码锁屏时，优先使用按实际屏幕尺寸换算的 1.9 秒慢速上滑，再按需尝试 Back 和兼容手势。已亮屏不按 Power，已可用的目标应用页面保持原样，USB 弹窗继续自动关闭并核验。准备位于业务脚本及 Trace/HiLog/PMU 启动之前，本机、Bridge、自定义计划和下载 Python 共用，沿用 45 秒准备及独立 20 秒失败取证预算。策略为 `screen_preparation/v5` / `power_key_stateful/v2`。本次更新服务器并重启即可；Windows 已运行 Bridge `1.4.7` 时无需重启，旧版仍按上一段升级。

平台 `0.1.8` / Runner `1.2.5` 补齐 PMU 采样器的构建与自动准备：部署时使用完整 OpenHarmony Native SDK 编译仓库固定源码，或导入已构建的完整 bundle；采集时复用设备已有程序，默认路径缺失时通过本机 HDC 或 Windows Bridge 安装经校验的版本。构建包记录源码、工具链及二进制哈希，页面展示资产状态与准备结果。当时首次使用需配置构建包；自 0.1.10 起由正常部署自动准备，见 [PMU 部署说明](DEPLOYMENT.md)。仅运行 `npm run build` 不会生成设备程序。Windows Bridge 已为 `1.4.7` 时无需因本次更新重启；能力探测通过不代表硬件事件或 perf 权限已验证。

平台 `0.1.9` 将 PMU 对外命名统一为“通用线程 PMU 采样器”，源码和新构建入口为 `thread_pmu_sampler.c` / `thread_pmu_sampler`，兼容原有 1.1.0 构建包与设备路径。同一程序可用于不同应用，按每个场景的 `profile.bundle` 定位进程；页面直接展示应用包名和启动 Ability，新建自定义场景清空图库应用及选择器，需填写目标应用或由助手补充。当前范围仍为已运行应用的唯一主线程用户态计数，不包含所有工作线程，也不完整覆盖冷启动。

平台 `0.1.10` 将编译好的 PMU 采样器及构建凭据纳入仓库 `tools/scenario_capture/prebuilt/aarch64-linux-ohos/`。常规 `heteroctl install/start/restart` 自动校验并准备 runtime 采样器资产，已有匹配版本直接复用；服务器无需完整 Native SDK，也无需单独传包或手动向手机安装。采集时继续通过本机 HDC / Windows Bridge 自动下发与复用。仅维护者修改 C 源码时需要重建并一起提交预置包，测试会核对源码与预置产物是否匹配。

平台 `0.1.11` / Runner `1.2.6` 优先尝试通过 `aa start` 拉起当前场景的目标应用，并读取新布局与电源状态确认解锁；图库 Ability 不存在时兼容 `com.huawei.photos.MainAbility`。保留 Power、标准唤醒、Back、动态上滑和 USB 弹窗处理。采集期间临时延长自动熄屏时间，结束、失败或取消后恢复原覆盖值；脚本传完后再次核验页面与 PID，变化时重新生成动作再核验。45 秒是每次屏幕准备的预算，就绪即继续，不是固定等待。若解锁提前拉起了被测应用，启动结果明确标记为非冷启动。更新服务器并重启即可，Bridge `1.4.7` 无需因本次更新重启。

平台 `0.1.12` / Runner `1.2.7` 取消初次屏幕准备与临采检查的累计 45 秒预算，保留单次命令超时、取消和有限重试。多次正常远程往返不再因累计耗时失败，临时防熄屏与结束恢复继续有效。并行寻优新增按物理核心组合的独立推荐：每个组合分别求最低时延或预算内最低频点，展示各组合频点、时延及相对最快方案的差距；部分组合无法达标时保留最佳可达时延并标注不可行。原全部配置列表与历史搜索继续可查。

平台 `0.1.13` 将负载特征按 BuildItem 的调度耗时与运行期 PMU 分层展示。调度账目区分 Running、抢占后等待、其他就绪等待、Blocked 和未知区间；抢占后等待属于 Runnable，Binder 阻塞属于 Blocked，不重复计时。PMU 新增自动多组采集，尽可能采集设备支持的缓存、分支、停顿和地址转换事件；不支持的事件保留诊断，旧单组计划继续可用。页面区分精确计数、阶段边界区间与复用估计，并展示覆盖率和实际采样跨度。新的采样器随仓库部署自动准备，已有历史数据不会补造缺失事件。详见[负载特征说明](docs/workload_features_model.md)。

平台 `0.1.14` / Runner `1.2.9` 在图库自动准备时先执行 `aa force-stop <bundleName>` 并确认进程退出，再启动并验证“照片 / 相册”主页；恢复的编辑页或弹窗通过有限次返回处理，持续异常保留具体诊断。重置运行状态不会清除应用数据或媒体文件，“已准备”模式保留当前页面。若选中启动步骤采集，关闭进程和返回桌面均在测量前执行，启动动作仍留在采集窗口内；记录反映实际进程及提前启动状态，不把应用重启称作整机冷启动。启动前没有可用 PID 时，PMU 如实报告未定位目标，不补造计数。

同一版本在开始采集前检查平台上次留下的采集会话，并在失败或取消后按归属回收采集器。仅处理能确认属于平台会话的进程和 Trace，已有输出保留；无法认领的旧采集或外部 Trace 保留并给出诊断，清理未确认时不会放行新采集。设备会话租约用于可续期的并发归属管理，不是任务总时限。

平台 `0.1.15` / Runner `1.2.10` 修复部分设备没有 `tr` / `awk` 导致 PMU 能力核验及采集会话识别失败的问题；能力结果按完整标记读取，兼容 HDC 额外诊断与被压缩的空行。旧默认路径采样器不支持多事件组时，自动安装并核验仓库随附版本；具体探测原因保留在采集详情中。能力核验确认程序协议，硬件事件与 perf 权限仍由实际采样确认。

执行按钮旁新增默认开启的“失败后自动修复并重试”：本次手动发起的采集失败或 PMU 采集器异常时，将诊断交给当前 Agent，修复后沿用设备与采集范围重试一次；历史记录、用户取消和普通覆盖不足不触发。关闭选项会撤销待执行请求，已经启动的自动重试进入取消流程，完成后才释放设备。修复请求、原任务与重试任务均持久关联，避免重复提交。

负载特征中的阶段指令数、周期数按有效采样窗口与阶段的重叠比例估算，复用计数先按 enabled/running 校正，主表显示“≈ 单值”。未覆盖的时间不外推，原始计数范围、覆盖率及估算方法保留在详情中。已有特征版本保持原样；保留原始 Trace 与 PMU 文件的数据集可重新“提取负载特征”获得新估算。

平台 `0.1.16` / Runner `1.2.11` 为电源查询增加 `hidumper -s 3301 -a -a` 兼容路径。在 `power-shell dump -s` 不支持的设备上，唤醒判断、临采电源核验及自动保亮均读取真实系统状态；保亮仍按保存的原始覆盖值恢复。命令成功与电源已亮均不能替代新鲜 UI 布局的解锁核验。

新增内置示例“图库 · 编辑页 · 全事件组 PMU 归档 · 复用估计”，保留旧缓存单组归档。新归档含 26 个 BuildItem、7 组 PMU 及阶段加权估值，解析数据与原始 Trace/PMU 压缩包都随 Git 仓库和正常部署提供。该轮 14 种请求事件均成功打开，但分支指令计数为零，硬件语义待核验；扩展组运行覆盖约 16.6%，实际采样中位间隔约 159 μs，不能视为每阶段全事件精确计数。新样本按已核验的样本指纹自动关联参考硬件，支持负载分析与条件寻优；PMU 质量限制不变。

服务器演示无需连接手机、Windows Bridge 或配置 Agent：进入「并行建模」→「负载归档」，选择“图库 · 编辑页 · 全事件组 PMU 归档 · 复用估计”。首次使用随版本样例时点击「载入历史样例」；已有归档可直接选择。随后在「负载特征」的统一数据集选择器中选中该归档，并点击「提取负载特征」。载入时自动校验并展开原始 Trace/PMU；「Trace 管理」可查看和下载该示例的原始 Trace。新旧两份图库归档均可演示并行寻优；新示例已导入的数据、分析和特征版本无需重建。搜索时核验完整解析内容与 Trace 指纹，再采用同设备历史拓扑及本轮频点证据匹配参考硬件。结果保留硬件匹配依据、代理模型来源和未做应用并行实测验证的说明。

原始包：[gallery-edit-all-pmu-20261023.zip](data/parallel-modeling/raw/gallery-edit-all-pmu-20261023.zip)（约 5.8 MB，普通 Git 文件，无需 Git LFS）。内含原样 Trace、PMU CSV/元数据、时钟对齐证据、采集质量记录和逐文件 SHA-256 清单；不包含图库 HiLog、图片或本机采集计划。运行时副本位于 `HETERO_STATE_ROOT/parallel-modeling/inputs/<dataset_id>/`，删除 Trace 副本后可再次导入恢复；仓库中的示例包保持不变。

采集时从目标设备布局动态读取屏幕尺寸，点击与手势适配实际宽高，参考手机分辨率不再限制其他设备。HiTrace 分类用空格分隔，执行参数随批次保存。PMU 目标间隔支持 10–10000 μs，默认仍为 100 μs；使用 10 μs 需设备端采样器支持，实测精度以采集报告为准。服务器升级需重启后台服务；设备端采样器构建、版本检查及更新方法见 [采集器说明](tools/scenario_capture/README.md)。

「负载特征」以 BuildItem 为阶段，展示计算、访存、调度与系统服务指标、采样边界和候选分型；可补充具有同线程、同时钟、阶段边界及事件语义的实测计数，保存为数据集绑定的特征版本。完整 PMU 不足时，平台仍根据已有 IPC、Cache/LLC MPKI 或有效非零基础计数生成带置信度和缺失证据的 Running 候选；只有完全没有有效定量证据时保留 `unknown`。通用 Cache 指标形成的候选不等同于已证实的 LLC/DRAM 瓶颈。并行建模选择该版本后，结合校准矩阵按运行中的任务组合计算干扰。参见 [负载特征模型](docs/workload_features_model.md)。阶段总览可导出阶段 CSV（耗时、调度、PMU 数值）、PMU 明细 CSV（估值、范围、覆盖率）和原有 JSON；CSV 可直接用 Excel 打开。

「并行建模」可直接使用随仓库提供的图库实测归档，也可导入采集批次或 Trace/PMU。页面展示 BuildItem 拆解、关键路径与负载特征，通过离线模型搜索 worker 数、主线程参与和共享核组频率，输出推荐配置、预计耗时与资源代价。该流程以数据和仿真为输入输出，不依赖应用源码。内置模型为带校准来源的条件预测，页面保留实测基线与模型预测的区别。

数据与模型位于 `data/parallel-modeling/`，采集器及 PMU 源码位于 `tools/scenario_capture/`；运行记录保存到 `HETERO_STATE_ROOT`。参见[场景采集与建模接口规范](docs/scenario_capture_parallel_modeling_spec.md)和[模型实现说明](docs/parallel_modeling_model_v1.md)。

「Trace 管理」展示原始文件的各个位置、大小与状态，支持下载、复制路径和删除指定副本。新版 Windows Bridge 将采集的原始 Trace 保存在持久目录，供 Windows 本地工具分析；平台分析副本单独管理。本机直连保留在本机，历史上传及采集数据按现有记录索引。

「寻优校准」以任务类型频点性能曲线、负载组合干扰矩阵和 Worker 任务派发开销为主校准项。compute / memory / mixed 三类代理按核组使用至少 5 个训练 OPP 拟合 CPU 频率—时延连续分段曲线，并以另 1 个未参与拟合的中间 OPP 独立验证；每类因此总计至少需要 6 个不同实测 OPP。方案覆盖硬件最低/最高 OPP，曲线只在训练域闭区间内使用，域外回退旧频率弹性。真实阶段的 PMU 候选分型只负责选择已实测曲线，两种频响不叠乘。有向双任务矩阵匹配阶段类别、核组和双方频点；未知组合及更高并发显式回退通用争用模型。Worker 开销使用同一常驻线程、同一 CPU、同一 OPP 和同一 payload，对比批内直接执行与逐项队列派发的往返 wall 差分；线程冷启动不纳入本轮，后续单独标定。详见 [双任务干扰校准](docs/parallel_pairwise_interference_v1.md)。页面可生成离线微基准方案与源码包，导入实测 JSON，检查样本与留出误差，再保存为可选寻优模型版本；底部曲线矩阵按实际核组和任务类型展开，显示聚合后的实测 OPP 点与通过质量门禁的拟合线。离线模型包的完整性与适用边界见[离线校准模型包](docs/offline_calibration_packages.md)，OPP 与流程说明见[Trace 管理与寻优校准](docs/trace_storage_and_calibration.md)。

## Run Locally

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
npm ci

# 默认 Agent 需要 HETERO_KIMI_BIN；也可选择已安装的 OpenCode 或自定义 Agent CLI。

.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8006 --workers 1
npm run dev -- --port 4032
```

打开 `http://localhost:4032`。浏览器默认使用同源 `/v1`，Next 在本地把它代理到 `HETERO_NEXT_API_PROXY_TARGET`，默认值为 `http://127.0.0.1:8006`。轻量部署继续使用该同源代理；可选加固部署才需要 Nginx 直接代理 `/v1`。

Linux + Windows 浏览器的默认部署同样不需要系统服务：

```bash
./deploy/linux/heteroctl install
./deploy/linux/heteroctl start
```

部署默认使用 `--remote`，Windows 直接打开 `http://<Linux服务器IP>:4032`；使用 `--local` 时 Web 只监听 Linux 回环地址。默认 Web 端口可通过 `install` 或 `restart` 的 `--web-port PORT` 为当前 runtime 持久化修改。`install` 会在 runtime 内自动运行 Kimi 官方安装器、安装最新版并独立校验 SHA-256。`start` 会启动 FastAPI、由 FastAPI 拉起 Kimi，再启动 Web，不需要单独执行 `kimi web`。后端未配置模型时，用户可在页面助手设置中填写自己的模型 URL、模型名和 Key。所有运行数据默认位于仓库下的 `heterogeneous-enable-web-runtime/`，也可用 `--runtime-root` 指向任意非系统用户目录。完整步骤见 [轻量用户态部署指南](DEPLOYMENT.md)；systemd/Nginx 仅作为可选加固拓扑保留。

## Runtime Resources

工具链和外部资源优先通过环境变量配置。部署控制器会在每次
`init/install/start/restart` 时校验当前命令环境中的非空覆盖值，并原子写入所选
runtime 的 `platform.env`；因此不需要 `--refresh-env`，后续命令也不需要重复
`export`。例如：

```bash
export HETERO_ROOTLESS_RUNTIME_ROOT=$PWD/heterogeneous-enable-web-runtime
export HETERO_LLVM_BIN_ROOT=/path/to/linux-host/llvm/bin
export HETERO_GLSLANG_BIN=/path/to/glslang
export HETERO_TRACE_STREAMER=/path/to/trace_streamer
export HETERO_OPENHARMONY_SDK_ROOT=/path/to/openharmony-sdk
export HETERO_RUNTIME_LIBRARY_ROOT=/path/to/all/aarch64-linux-ohos/libraries

./deploy/linux/heteroctl restart
./deploy/linux/heteroctl status
```

Linux Build Worker 的 LLVM 目录须包含可执行的 `clang`、`clang++` 和 `ld.lld`。模块代码分析使用平台内置的 Tree-sitter/SQLite 底图与 Agent 原生源码工具。
旧的命令行参数仍作为兼容入口：

```bash
./deploy/linux/heteroctl restart \
  --runtime-root "$HETERO_ROOTLESS_RUNTIME_ROOT" \
  --llvm-bin-root /path/to/llvm/bin \
  --library-root /path/to/all/aarch64-linux-ohos/libraries
./deploy/linux/heteroctl status --runtime-root "$HETERO_ROOTLESS_RUNTIME_ROOT"
```

优先级为：显式兼容参数、当前命令环境变量、已有 `platform.env`、runtime 默认值。
仅执行 `export` 不会改变已经运行的进程；随后执行 `start/restart` 会持久化配置并让
FastAPI/Build Worker 使用新值，资源变化时会自动重启其已有进程。

从现有 OpenHarmony checkout 提取紧凑 SDK 包：

```bash
./deploy/linux/heteroctl sdk --source-root /path/to/openharmony
```

输出固定为
`$HETERO_RUNTIME_ASSET_ROOT/openharmony-sdk`，并包含逐文件哈希
manifest。`--source-root` 也可指向直接包含 `openharmony/` 的父目录；完整
生成和跨机器传输步骤见 [轻量部署指南](DEPLOYMENT.md#7-可选运行资产)。

GPU、NPU、媒体等动态 profile 共用一个管理员审阅的目标 AArch64
OpenHarmony 运行库目录。平台只登记目录与逐文件哈希，不按 profile 复制库：

`HETERO_RUNTIME_LIBRARY_ROOT=/path/to/all/aarch64-linux-ohos/libraries` 会在
`start/restart` 时完成登记；`--library-root` 和
`runtime-libs --library-root ...` 作为兼容入口保留。

profile 仍只允许链接自身声明的固定库名；一次登记后，`build.describe_profile`
会分别报告各 profile 的 ready/missing 状态。

运行部署预检：

```bash
./deploy/linux/heteroctl preflight
./deploy/linux/heteroctl preflight --require-trace --require-device
```

## Agent And Device Boundary

- 每个浏览器页面 `page_session_id` 绑定一个可恢复的 Platform Run，`analysis_id` 只是该页面内可切换的分析上下文；所选 Agent CLI 在同一 Run 工作区中延续上下文，页面刷新或后端更新后通过稳定 Run 绑定和原生 Agent Session 恢复。
- 模型供应商凭证只持久化在当前浏览器；后端使用可撤销的短期内存租约启动 Kimi，不写入 Kimi `config.toml`、Platform artifact、Run event 或日志。
- 每个 Run 使用隔离 workspace、版本化 Skill 和 run-scoped MCP token；路由和机会推理为结构化只读阶段。
- 真机动作链为 `build.describe_profile` → `build.stage_source` → `build.create` → `device.deploy_artifact` → `device.execute_shell`；构建事实和设备执行事实分别保留。
- Desktop-first 默认链路由 Linux Worker 编译并产生带 SHA-256 的工件，Windows Desktop 拉取、复核并通过本机 HDC 执行。0.1.41 已交付服务器环境合同，长任务和跨机工件自动传输仍按设计中的 typed job/artifact 协议演进。
- 0.1.63 的 Desktop Agent 运行证据归本机 Global Host；Workspace 只引用共享 Trace、硬件模型、流程和知识资产，Window Instance 隔离 Agent PTY/token、草稿、任务、终端和编辑缓冲。当前仍为单窗口，多窗口需先完成三层 Runtime 拆分和全局设备租约。
- 既有 Web 入口仍可使用 Windows HDC Device Bridge 主动连接 Linux 后端；这是兼容链路，不是 Desktop 本机直连设备的必经路径。
- 构建成功、上传成功、设备零退出码、功能等价和性能收益是不同证据，平台不会互相替代。

第一次部署请直接阅读 [轻量用户态部署指南](DEPLOYMENT.md)。新增 OpenCode 或团队 Agent 时阅读 [Agent 框架接入指南](docs/agent_framework_adapter_guide.md)，其中给出了 manifest、工作区、Skill、MCP、模型配置、会话恢复和完整 AgentEngine 接口。完整说明见 [总体技术文档](docs/heterogeneous_optimization_enablement_platform_technical_overview.md)、[Agent Runtime 与 Device Bridge Spec](docs/platform_agent_runtime_build_device_bridge_spec.md) 和 [可选加固部署细节](deploy/README.md)。修改代码、Skill、知识库或部署拓扑前，请按 [文档与实现同步规范](docs/documentation_change_control.md) 检查受影响契约与回归门禁。

## Verification

```bash
.venv/bin/python -m unittest discover -s backend/tests
.venv/bin/python tools/analysis/validate_heterogeneous_kb.py \
  --kb data/knowledge-bases/patterns \
  --structure-only
npm run build
```

发布知识库时必须另外挂载它所绑定的 OpenHarmony revision，并运行带
`--source-root /path/to/openharmony --strict` 的完整证据校验；部署包中的
`--structure-only` 只校验 schema、交叉引用、manifest 和内容哈希。

当前仍是单用户产品形态。未实现用户认证前，只应部署到受控内网或 VPN，不应直接暴露到公网。
