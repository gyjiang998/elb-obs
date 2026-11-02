# 异构优化使能平台工程问题复盘

> 文档状态：Engineering Retrospective v1.1
> 历史统计区间：2026-07-15 至 2026-08-26；2026-10-27 增补设备链路专项复盘（第 10 节）
> 复盘对象：异构优化知识库、Trace 热点归因、机会推理、Agent Runtime、真机验证、跨主机部署与应用流程分析
> 第 2–9 节的 76 个问题族、测试/文件数量和“已解决”状态是历史快照，不代表 10 月后的全量统计；当前执行拓扑见 [桌面工作台设计与架构复核](desktop_workbench_design.md)。

## 1. 为什么要做这份复盘

平台从“异构案例与 CPU fallback 分析”逐步演进为包含 Trace、源码、知识库、Agent、Build Worker、Device Bridge 和真机证据的完整系统。过程中出现的问题并非零散 bug：很多问题揭示了模型推理、静态证据、构建环境和分布式设备之间的真实工程边界。

本复盘从会话 JSONL 中抽取用户问题和已验证结论，目的是回答四件事：

1. 我们实际遇到了多少类问题，而不是进行了多少次对话。
2. 每类问题的根因是什么，最终采用了什么解决方式。
3. 哪些经验可以沉淀为架构、契约、Skill、模板或回归测试。
4. 哪些能力仍处于缓解或持续建设状态，不能被描述成已经完全解决。

原始 JSONL 可能包含本机路径、设备标识、模型配置和中间工件，因此不进入 Git 仓。本文只保留脱敏后的工程事实。

## 2. 数据来源与统计口径

### 2.1 会话数据

| 指标 | 数值 | 说明 |
|---|---:|---|
| 会话谱系起点 | 2026-07-15 | 当前 rollout 从早期会话 fork 并保留历史 |
| 当前 JSONL 起点 | 2026-08-06 | 主 rollout 文件的创建时间 |
| JSONL 记录 | 98,335 条 | 包含消息、工具调用、输出、计划和运行事件 |
| JSONL 大小 | 约 689 MB | 主要体积来自工具输出，不等于问题数量 |
| 原始 user message item | 517 条 | 包含环境注入与内部连续执行消息 |
| 清洗后的真实用户消息 | 409 条 | 去除环境、插件、goal continuation 和中断控制消息 |
| 完全相同文本去重后 | 385 条 | 仍包含同一问题的不同描述和重试 |
| 最终回答 | 419 条 | 用于交叉检查修复结论和验证结果 |

### 2.2 工程证据

截至本次复盘：

- 仓库自首个提交以来有 52 个提交、327 个文件发生过变更。
- 后端共有 483 项测试；最近一次全量执行全部完成，其中 2 项按环境条件跳过。
- 已形成 28 份平台级技术文档与专项 Spec，以及优化、硬件和 API 多层知识资产。

### 2.3 问题计数

409 条真实用户消息经过语义合并后，得到 **76 个主要工程问题族**：

| 状态 | 数量 | 判定标准 |
|---|---:|---|
| 已解决 | 69 | 当前仓库已有实现，并有测试、运行结果或明确文档证据 |
| 已缓解 | 6 | 主要失败模式已有处理，但规模、框架或部署差异仍需继续验证 |
| 持续建设 | 1 | 尚不存在足够通用的完成证据 |

这里的“已解决”表示对应工程问题已有稳定处理路径，不表示某个热点已经获得真实性能或功耗收益。性能收益仍需场景 Trace、功能等价和端到端 benchmark 证明。

## 3. 问题总账

### 3.1 架构与证据边界

| ID | 问题与症状 | 根因 | 解决方式 | 状态 |
|---|---|---|---|---|
| 01 | 知识库和机会报告曾共用一种输出 | 离线知识与在线场景结论生命周期不同 | 拆为优化知识库、硬件知识库、API 知识库和机会报告工件 | 已解决 |
| 02 | Trace 团队输出、原始 `.htrace` 和平台职责混杂 | 输入生产者与机会识别器边界不清 | 明确 Trace 报告和原始 Trace 都是输入，平台负责热点到源码再到机会 | 已解决 |
| 03 | 一份 Spec 同时描述 Trace、Web 和 KB | 所有权、验收和演进节奏不同 | 建立 Trace 技术、机会/知识库、Web/Agent 三类权威 Spec 及共同契约 | 已解决 |
| 04 | 页面以 Demo Trace 作为主路径 | POC 默认值掩盖真实输入失败 | 删除 Demo 主叙事，真实 Trace/报告成为首要入口 | 已解决 |
| 05 | 模型假设、静态匹配和真机事实容易混写 | 缺少统一证据等级 | 建立 observed、source-backed、model-hypothesis、device-verified 等分级 | 已解决 |
| 06 | Shader 和 C 编译成功曾被描述成“代码逻辑正确” | 编译、链接、执行、等价和收益没有分层 | 明确各阶段证据，只允许通过的阶段提升对应证据等级 | 已解决 |

### 3.2 Trace、热点和源码归因

| ID | 问题与症状 | 根因 | 解决方式 | 状态 |
|---|---|---|---|---|
| 07 | `.sys`、JSON、分析报告和 `.htrace` 含义不清 | UI 暴露了历史内部格式 | 输入规范化为 Trace 团队报告或原始 `.htrace`，其他格式作为兼容输入 | 已解决 |
| 08 | 28,000 余条 wakeup 链被 FFRT/Binder 淹没 | 直接展示函数级长尾数据 | 增加模块聚合、系统模块折叠、筛选、滚动和 Top-N 展示 | 已解决 |
| 09 | 大量 Trace 簇显示没有 event 名称 | scheduler 链与 HiTrace span 并非总能一一关联 | 保留缺失状态，补充事件提取和 top function/module fallback | 已解决 |
| 10 | `M38`、`I38` 等标签未参与源码检索 | 未解析 HiTrace level/module 编码 | 建立模块目录解析，将 event 模块用于缩小源码检索范围 | 已解决 |
| 11 | duration 和 self duration 混用，累计 10 ms 被误判为热点 | 未考虑嵌套 span 和时间局部性 | 同时统计 inclusive、自耗时、单次峰值、发生窗口和调用次数 | 已解决 |
| 12 | 把 event name 当函数名搜索，真实桩点找不到 | event 字符串由插桩显式指定 | 优先搜索 HiTrace 宏字符串，再定位桩点所在语句、函数和调用边界 | 已解决 |
| 13 | ArkUI event 找不到源码 | 搜索范围、模块映射和宏封装没有联动 | 用模块到代码范围映射、宏参数反查和 wrapper 定位扩展解析 | 已解决 |
| 14 | 工作树未提交就不能建立精确源码绑定 | revision 门禁把 commit 当作唯一快照 | 对当前文件内容计算 hash 并建立源码快照，允许 dirty worktree | 已解决 |
| 15 | Trace 桩点所在函数未必是真正热点，耗时可能在深层 callee | 只有 span、没有采样栈时无法直接分摊 CPU 开销 | 建立 anchor、候选代码片段和 callee 深挖流程；无采样时保留推测等级 | 已缓解 |

### 3.3 优化知识库与静态代码分析

| ID | 问题与症状 | 根因 | 解决方式 | 状态 |
|---|---|---|---|---|
| 16 | “发现异构符号”被误当成异构+CPU pair | 候选扫描条件过宽 | 严格要求异构实现、CPU/software fallback、控制关系和源码证据闭环 | 已解决 |
| 17 | 238 条候选曾被理解为都已确认 | candidate 与 source-confirmed 状态不醒目 | 分离候选、闭环和正式知识案例，页面只展示正式异构案例 | 已解决 |
| 18 | Pair 扫描过错误的代码范围 | 大仓根目录和模块根目录混淆 | Connector 显式绑定仓库根、扫描范围、内容 hash 和 provenance | 已解决 |
| 19 | AST 分析只看函数名字，没有分析实现体 | 早期提取器偏词法索引 | 解析函数定义、函数级 AST/CFG 和编译上下文，记录 profile 证据等级 | 已解决 |
| 20 | ASTC wrapper 的 AST 不能代表 `astcenc_compress_image` 的真实负载 | wrapper 与计算核心跨调用边界 | 增加计算核心门禁和 callee 追踪，不能解析核心时标为 not-ready | 已缓解 |
| 21 | 知识案例缺少 CPU 数据类型、访存和控制特征 | 案例只记录“用了什么硬件” | 为严格案例补充 CPU 代码、AST profile、特征说明和硬件适配原因 | 已解决 |
| 22 | 硬件知识只有单元名称，无法落到可调用 API | 单元能力与软件接口脱节 | 建立 NPU、GPU、DSP、媒体 API 库，包含摘要、约束和验证协议 | 已解决 |
| 23 | 自建 API 扫描大量命中 `PreparedModelService` 等内部实现 | 未区分公开合同、HDI、服务实现和测试代码 | 合同优先分层扫描、质量门禁、独立自建库，并动态加入路由与 API 检索 | 已解决 |

### 3.4 模型推理、RAG 与 Skill 工程

| ID | 问题与症状 | 根因 | 解决方式 | 状态 |
|---|---|---|---|---|
| 24 | 直接问大模型有时比平台更快、更准确 | 平台长上下文和硬规则抵消了知识资产价值 | 保留模型先验，以知识、证据、模板和复现能力提供可审计增益 | 已解决 |
| 25 | reasoning context pack 包含大量 hash、空数组和重复元数据 | 审计对象被原样塞进模型上下文 | 建立损失感知投影，只传任务所需事实，完整工件留在审计层 | 已解决 |
| 26 | 模型返回 JSON 缺逗号、尾随文本或没有 JSON 对象 | 结构化合同只依赖模型一次生成 | 增加提取、校验、有限修复和 evidence-insufficient 降级 | 已解决 |
| 27 | 8K/4K 输出截断、2 MiB relay 限制和 240 秒超时 | UI 设置、Agent Engine、relay 和供应商上限不一致 | 统一 token 配置、流式响应、分轮任务、compaction 和大小诊断 | 已解决 |
| 28 | Skill 规则越来越长，模型速度和泛化下降 | 每次事故都向 Skill 增加 corner case | 只保留目标、必要契约和停止条件，把环境知识迁移到工具与 profile | 已解决 |
| 29 | Skill 迭代后无法回到旧效果 | 缺少版本化和评测资产 | 建立 Skill profile/version、用户编辑分支、热加载和回归评测 | 已解决 |
| 30 | 改函数名后 DSP/GPU 路由失效 | 词法规则实际上提前完成了路由决策 | 规则降为审计基线，模型依据代码画像和硬件能力卡进行语义路由 | 已解决 |
| 31 | 逐条比较所有硬件 API 成本过高 | 检索与精核没有分层 | 先选硬件族，再按摘要检索 Top-N API，最后精确比较合同 | 已解决 |
| 32 | 不知道知识库和 Skill 是否真的优于裸模型 | 缺少独立对照 | 增加隔离盲推理、流程结果对照、正负样本和变换泛化评测 | 已解决 |
| 33 | 长推理只显示“等待模型响应”，且思考语言不一致 | 流式事件没有统一投影到进度区 | 将模型增量、工具动作和阶段状态统一投影到实时分析进度 | 已解决 |

### 3.5 Agent Runtime 与会话控制

| ID | 问题与症状 | 根因 | 解决方式 | 状态 |
|---|---|---|---|---|
| 34 | 点击推理或助手发送后没有动作、曾出现 404 | 页面动作和后端 endpoint/状态机漂移 | 统一 action catalog、运行入口和前后端契约测试 | 已解决 |
| 35 | 页面编排过细，Agent 只能完成固定步骤 | page-centric loop 限制原生 Coding Agent 能力 | 转为 agent-centric：页面提交意图，Agent 自主使用后端工具并回填 UI | 已解决 |
| 36 | 路由、机会推理、真机验证之间上下文丢失 | 每阶段创建独立会话 | 一个页面绑定一个持久 Agent Session，独立盲推理除外 | 已解决 |
| 37 | HTTP 429 后“继续工作”从路由重新开始 | 只保存 UI 阶段，没有保存 Agent checkpoint | 持久化 Platform Run、Agent events、工件和恢复意图 | 已解决 |
| 38 | 固定 3 轮或 20 轮仍不能完成真机任务 | 轮次由页面而非 Agent 任务状态驱动 | 改为自主迭代，用户只控制上限、停止和继续 | 已解决 |
| 39 | 失败后无法知道每轮提示、响应、工具和页面动作 | 缺少统一 observability trace | 落盘 Agent Run 事件流，关联模型、action、artifact 和错误 | 已解决 |
| 40 | “Kimi CLI”窗口只是样式模拟，能力不等于真实 CLI | 页面聊天框没有 PTY 和后端进程 | 接入真实后端 PTY/WebShell，保留终端输出和输入语义 | 已解决 |
| 41 | OpenCode/自研 Agent 找不到 MCP，或只会 grep action 名称 | 适配器继承了 Kimi 专属描述和发现方式 | 建立厂商中立 CLI Adapter、可移植 Skill/MCP 工作区和 task/action 契约 | 已解决 |
| 42 | Agent 工作时页面被强制滚到底部，停止后仍继续 | UI 滚动、取消请求和后端 run 生命周期未对齐 | 分离 follow-output、用户滚动和 cancel；结果继续保留为审计事实 | 已解决 |

### 3.6 真机实验、Build Worker 与硬件运行时

| ID | 问题与症状 | 根因 | 解决方式 | 状态 |
|---|---|---|---|---|
| 43 | 平台找不到已经安装的 HDC | PATH、部署环境和本地/远端设备位置不同 | 支持受管环境注册、HDC Bridge 和明确的 device discover 诊断 | 已解决 |
| 44 | CPU smoke 被误解为 DSP 卸载成功 | 示例只证明输入输出和设备执行 | 区分 CPU reference、硬件 API 调用、硬件参与和性能收益 | 已解决 |
| 45 | 任意热点被固定 Adapter 合同拒绝 | 安全模板被错误用作通用 Agent 编码接口 | 增加 Agent 自写完整实验源码、固定 Build profile 和独立设备执行 | 已解决 |
| 46 | Agent 把 `art_*` 当成本机文件路径读取 | artifact identity 与 workspace path 未分型 | 路由器显式拒绝 artifact-as-path，并提供读取工件的正确 action | 已解决 |
| 47 | Agent 反复删除 `unistd.h`、`stdio.h` 等标准头文件 | 固定 SDK 缺失且错误被解释为源码问题 | 构建完整 musl header surface，一次返回全部缺失项并停止源码重写 | 已解决 |
| 48 | 换机器后 SDK、LLVM、glslang、HDC 配置无法复现 | 本机绝对路径和手工资产未进入部署闭包 | 提供紧凑 OpenHarmony SDK bundle 生成器和部署预检 | 已解决 |
| 49 | 每个 GPU/NPU profile 单独复制运行库，配置复杂 | 运行库组织与 profile 强绑定 | 支持管理员共享 AArch64 runtime library root、hash registry 和快照链接 | 已解决 |
| 50 | x86 Linux 编译机上误选 x86/arm 库 | 混淆 host toolchain 与 target ABI | 固定 AArch64 OpenHarmony target，校验 ELF machine 和库架构 | 已解决 |
| 51 | Vulkan 直接链接后首次调用静默退出 | 设备 loader/部署合同与普通 ELF 直链假设不同 | 沉淀 `dlopen + dlsym` 模板，并保存为运行时限制证据 | 已解决 |
| 52 | `ld.lld` 报 `thread constructor failed` | `RLIMIT_NPROC=16` 按共享 real UID 计数，误伤链接线程池 | 移除该限制、固定 `--threads=1`、升级资源策略并分类为基础设施故障 | 已解决 |
| 53 | Agent 每次从零摸索 Vulkan/NNRT/Harness，耗时且易失败 | 成功实验未沉淀为可复用资产 | 建立 API 语义族模板、curated harness 和 template-blind 回归 | 已缓解 |
| 54 | 任意 NPU、DSP、媒体热点尚不能稳定自动生成并完成真机等价验证 | API/provider、输入合同和测试承载差异很大 | 已建立扩展契约与部分模板；仍需按语义族持续补齐真实 recipe | 持续建设 |

### 3.7 部署、跨主机和模型服务

| ID | 问题与症状 | 根因 | 解决方式 | 状态 |
|---|---|---|---|---|
| 55 | Linux 服务只监听 `127.0.0.1`，Windows 无法访问 | 本地与远程模式未区分 | `--local/--remote` 明确绑定策略，远程模式仅暴露 Web 入口 | 已解决 |
| 56 | 3006 端口通用且冲突，用户不能配置 | 端口写死在脚本和代理中 | 默认改为 4032，并支持环境/命令配置和代理联动 | 已解决 |
| 57 | `/v1/health` 返回 400 | Host/代理边界与部署地址不匹配 | 修正可信 host 与 Web API proxy，健康检查覆盖直连和代理路径 | 已解决 |
| 58 | 运行中重装后静态 chunk 404、页面错乱或 500 | `.next`/`node_modules` 被非原子替换，旧进程仍引用旧产物 | 安装前停止服务，构建后再切换；禁止边运行边覆盖 runtime | 已解决 |
| 59 | `heteroctl stop` 太软，残留 3006/8006 进程 | 只依赖过期 pidfile | 增加端口所有者核验、终止和二次确认 | 已解决 |
| 60 | npm/Kimi 安装长时间无输出，看似卡死 | 下载和依赖安装缺少心跳与当前命令 | 超过一分钟打印命令与状态，支持缓存、超时和官方安装脚本 | 已解决 |
| 61 | 浏览器保存模型配置但 Kimi Agent 未生效，HTTP base URL 被拒绝 | 浏览器、FastAPI、relay、Agent Engine 使用不同配置源 | 后端托管服务并接受浏览器临时 override，刷新 Agent 配置并真实 probe | 已解决 |
| 62 | Agent 在 Linux，手机连 Windows，后端无法直接 HDC | 设备执行跨越浏览器客户端边界 | Windows Device Bridge 负责 HDC，Linux Build Worker 负责构建，工件经受控 RPC 传输 | 已解决 |
| 63 | `HETERO_LLVM_BIN_ROOT`、glslang 等 export 后不生效 | 环境只在启动进程读取且配置入口分散 | 统一持久环境文件、`refresh-env`/restart 和 preflight 展示 | 已解决 |
| 64 | 无 Git 环境无法更新，安装写入 `/opt`、`/var` 过重 | 部署假设面向管理员主机 | 支持源码压缩包、用户目录 runtime 和无 root 安装 | 已解决 |
| 65 | 担心 Agent 把代码发送给 Web Search/Fetch | Coding Agent 默认工具能力不透明 | 部署预检禁止 Web Search/Fetch，只保留模型推理所需网络 | 已解决 |
| 66 | 平台升级后旧 Session 不能继续 | PTY 进程、Agent 内存状态和版本工件并非完全持久化 | Platform Run 和工件已持久化，可重建上下文；原 CLI 进程续接仍受升级边界影响 | 已缓解 |

### 3.8 页面体验、应用流程与扩展能力

| ID | 问题与症状 | 根因 | 解决方式 | 状态 |
|---|---|---|---|---|
| 67 | 左右导航滚动、表格过长，目录类按钮点击后才加载且容易重复提交 | 页面按数据块堆叠，只读目录和 pending 状态又绑定在点击之后 | 固定导航/助手，使用模块 Top-N；页面启动预热并合并目录请求，关键入口同步防重入 | 已解决 |
| 68 | 用户不知道模型是否在工作，也看不到 token 成本 | 流式输出、usage 和页面状态分散 | 实时进度统一展示模型与 action；状态栏统计 input/output/cache hit | 已解决 |
| 69 | Hydration、`crypto.randomUUID`、中文小字体和输入光标问题 | SSR/client 差异、旧浏览器兼容和终端样式叠加 | 客户端稳定初始化、UUID fallback、字体尺度修正和多行终端输入 | 已解决 |
| 70 | 切页面状态丢失，分析记录只能查看不能恢复 | 页面 state 未绑定持久工件 | 分析记录保存多轮上下文，可装载回推理工作台，并提供显式重置 | 已解决 |
| 71 | 平台流程发现十几分钟仍失败，直接 Kimi CLI 却很快 | 长 Skill、受限 action 和页面编排削弱 Agent | 精简 Agent-Centric Skill，让 Agent 直接读代码，页面只负责意图和结果投影 | 已解决 |
| 72 | Mermaid 和 JSON 同时由模型生成，格式易漂移 | 两份表示有两个生成源 | 模型只生成 Mermaid，确定性工具编译现有 Stage JSON 并补证据字段 | 已解决 |
| 73 | 流程 JSON 的 start/end 使用 C++ 函数签名而不是真实 Trace 名 | 将函数定位误当成插桩字符串 | 从 HiTrace 宏参数提取真实 event name，函数只作为 source anchor | 已解决 |
| 74 | 对 30 GB OpenHarmony 仓做完整 Tree-sitter/图谱扫描成本高 | 全量解析、存储和增量失效没有分层 | 已基准验证模块级 SQLite code graph；全仓需清单、分区和增量构建 | 已缓解 |
| 75 | Kimi 可自动提交，部分自研 Agent 无法粘贴、提交或发现 MCP | 不同 CLI 的 PTY、快捷键和工具协议并不等价 | 建立 Agent Adapter SPI 和 manifest；非 Kimi 框架仍需逐一做兼容验收 | 已缓解 |
| 76 | 用户自建硬件 API 库建成后没有进入路由和后续推理 | 路由单元目录只加载平台内置 KB | 动态加载 `custom:<kb_id>`，在路由、Top-N API 和删除生命周期中统一引用 | 已解决 |

## 4. 最值得复用的十二条经验

### 4.1 先定义证据，再设计自动化

模型可以提出 GPU、NPU、DSP 或并行方向，但只有源码事实、设备 capability、编译、执行、功能等价和 benchmark 能逐步提升证据等级。没有证据分级，自动化越强，错误结论传播得越快。

### 4.2 Trace event 是锚点，不天然是热点函数

HiTrace 名称通常是宏参数字符串，可能覆盖一个 wrapper、一个阶段或一组下游调用。正确流程是“event -> 桩点 -> 代码作用域 -> callee/数据流候选 -> 动态证据”，不能直接做“event name -> function name”。

### 4.3 AST 必须分析实现体和真实计算核心

函数名、dispatcher 和 wrapper 只能提供导航信息。可匹配画像需要函数定义、编译上下文、AST/CFG、访存、依赖和运行时边界；开销在 callee 时必须继续追踪，追不到就降低证据等级。

### 4.4 RAG 要分层检索，不能把整个知识库塞给模型

有效路径是先生成硬件中立代码画像，再选择硬件族，然后按 API 摘要检索 Top-N，最后读取少量完整合同。完整知识库、hash 和空字段只保留在审计工件中。

### 4.5 Skill 应描述任务和契约，不应复刻编排器

长 Skill 容易把历史事故、会话名称和工具细节固化成低信息约束。环境事实应由工具返回，流程状态应由 runtime 管理，Skill 只保留目标、必要输出、证据边界和停止条件。

### 4.6 一个页面 Session 是最自然的工作单元

硬件路由、API 精核、真机修订和用户追问互相依赖。把它们拆成多个模型会话会丢失决策理由；同一页面使用持久 Session，独立盲推理才使用隔离 Session。

### 4.7 Agent 必须有事件日志，而不只是聊天 transcript

一次分析应同时记录模型请求、增量响应、工具参数、工具结果、页面动作、artifact 和错误。这样才能区分“模型没想到”“工具没暴露”“环境失败”和“页面没有回填”。

### 4.8 Build surface 本身就是产品能力

真机实验失败经常与算法无关，而是 SDK、header、ABI、运行库、loader、线程限制或部署拓扑问题。Build Worker 必须提供固定 profile、完整诊断、内容 hash 和环境级错误分类。

### 4.9 跨主机设备不是普通后端设备

Linux 后端无法直接访问连接在 Windows PC 上的手机。正确架构是后端构建、客户端 Device Bridge 执行 HDC、artifact 受控传输，而不是暴露通用远程 shell。

### 4.10 模板能减少摸索，但不能替代验证

成功的 Vulkan、GLES、NNRT 或媒体实验应沉淀为语义族模板。模板可以复用脚手架和已知部署合同，但历史 PASS 不能证明当前设备、当前热点或当前性能收益。

### 4.11 部署更新必须原子化

运行中覆盖 `.next`、`node_modules` 或 Python runtime 会制造难以解释的 404/500。可靠更新顺序是停止、校验源码包、安装/构建到稳定目录、切换、启动、健康检查和回滚。

### 4.12 失败修复应提升泛化能力

遇到新错误时，优先修契约、工具诊断、环境发现和证据模型；不要把具体函数名、错误字符串或某个案例直接塞进 Skill。回归测试应覆盖等价改名、控制流变化和未见输入。

## 5. 平台演进脉络

| 阶段 | 核心变化 | 解决的主要问题 |
|---|---|---|
| 1. 方法与 Spec | 分离 Trace、KB、机会报告和 Web 平台 | 从概念 POC 变成有职责和验收边界的工程 |
| 2. 严格知识资产 | 建立 source-confirmed pair、函数画像和硬件/API KB | 从关键词命中变成源码与合同证据 |
| 3. 真实 Trace 工作台 | `.htrace`、wakeup 链、模块、自耗时、源码桩点 | 从人工翻 Trace 变成热点候选导航 |
| 4. 多策略推理 | RAG、硬件上下文、直接基线、Skill 评测 | 从单一规则变成可比较、可回滚的推理 |
| 5. Agent-Centric Runtime | 持久 Session、PTY、MCP/Action、运行事件流 | 从页面按钮编排变成 Agent 自主完成任务 |
| 6. 真机闭环 | Build Worker、固定 profile、模板、设备执行和证据校准 | 从“建议可行”推进到真实编译、下发和验证 |
| 7. 跨主机部署 | Linux Web/Build + Windows Device Bridge | 适配生产中浏览器、服务器和手机分离的拓扑 |
| 8. 流程与代码知识 | 模块 code graph、流程发现、Mermaid 编译和 HiTrace 锚点 | 从单函数热点扩展到应用执行流程和关键路径 |

## 6. 当前实现证据索引

| 能力 | 主要实现 | 主要测试/规范 |
|---|---|---|
| Trace 解析与模块聚合 | `backend/htrace_analysis.py`、`backend/trace_modules.py`、`backend/trace_analysis.py` | `docs/trace_analysis_technology_spec.md` |
| 热点到源码调查 | `backend/page_analysis_workflow.py`、`backend/source_revision_snapshot.py`、`backend/hitrace_instrumentation.py` | `backend/tests/test_real_trace_reasoning.py` |
| 静态画像与匹配 | `backend/generic_code_semantic_profile.py`、`tools/analysis/precise_feature_extractor.py` | `docs/function_code_profile_technology.md` |
| 优化与硬件 API KB | `data/knowledge-bases/`、`backend/accelerator_api_retrieval.py` | `docs/heterogeneous_optimization_knowledge_base.md` |
| 自建 API KB | `backend/custom_api_kb.py` | `docs/custom_accelerator_api_knowledge_base_spec.md` |
| Skill 版本与用户编辑 | `backend/skills.py`、`backend/skill_versions.py`、`backend/user_skills.py` | `backend/tests/test_skills.py`、`backend/tests/test_skill_versions.py` |
| Agent Runtime 与 PTY | `backend/agent_engine.py`、`backend/analysis_agent_session.py`、`backend/agent_cli_adapters.py`、`backend/kimi_cli_terminal.py` | `backend/tests/test_agent_engine.py`、`backend/tests/test_agent_cli_adapters.py` |
| 可观测性 | `backend/analysis_observability.py`、`app/components/AnalysisRecordTrace.tsx` | `docs/platform_agent_runtime_build_device_bridge_spec.md` |
| 真机构建与执行 | `backend/build_worker.py`、`backend/device_experiment.py`、`backend/device_gateway.py` | `backend/tests/test_build_worker.py`、`backend/tests/test_device_experiment.py` |
| Windows Device Bridge | `deploy/windows/install-device-bridge.ps1`、`deploy/windows/run-device-bridge.ps1` | `backend/tests/test_device_bridge_contract.py` |
| 部署控制面 | `deploy/linux/heteroctl`、`deploy/README.md` | `backend/tests/test_user_space_deployment.py` |
| 应用流程与代码底图 | `backend/application_flow.py`、`backend/code_graph.py`、`app/components/ApplicationFlowStudio.tsx` | `backend/tests/test_application_flow.py` |
| 页面 Agent 与实时进度 | `app/components/KimiWebShell.tsx`、`app/components/AgentPanel.tsx` | 后端 Agent API/Session 回归与生产构建检查 |

## 7. 推荐排障方法

后续再遇到问题时，按以下顺序处理：

1. **定位层级**：输入、Trace、源码、检索、模型、合同、Agent、构建、设备、部署还是 UI。
2. **保存原始事实**：保留 stderr、return code、action 参数、artifact ID 和时间戳，不先改写结论。
3. **寻找最小反例**：复现同一接口的最小成功/失败样本，区分代码问题和环境问题。
4. **核对证据等级**：确认当前只证明了编译、执行、等价还是收益。
5. **修公共机制**：优先修 schema、profile、adapter、诊断和状态机，避免增加案例特判。
6. **补回归**：至少覆盖原失败、一个相邻正例和一个不应通过的负例。
7. **更新文档**：涉及边界、配置、工件或状态语义时同步更新权威 Spec。
8. **记录限制**：成功后仍写明设备、API、输入规模和未证明事项。

## 8. 尚未结束的工作

1. 补齐更广泛的 NPU、DSP、媒体 API 真机 recipe、CPU oracle 和官方测试承载。
2. 在缺少 HiPerf/采样栈时，提高从 Trace anchor 向深层 callee 热点归因的校准能力。
3. 将模块级 code graph 扩展为 30 GB 大仓可增量、可分区、可失效的代码知识底图。
4. 进一步提高跨版本 Agent Session 恢复能力，明确 PTY 进程不可恢复时的重建语义。
5. 对 OpenCode 和自研 Agent 做与 Kimi 相同级别的粘贴、自动提交、MCP 发现和取消验收。
6. 持续积累真机功能、性能和功耗数据，避免把静态可行性当作优化收益。

## 9. 结论

这段工程历程解决的核心并不是“让模型说出 GPU/NPU”这么简单，而是把一个容易产生漂亮猜测的流程，逐步改造成能够保留输入、定位源码、检索合同、驱动 Agent、真实构建、跨主机下发并校准证据的系统。

76 个问题族中，最有价值的结果不是某个单点修复，而是形成了几条可持续原则：**知识与场景分离、事实与假设分层、Agent 与页面解耦、构建与设备分域、Skill 与工具分工、成功与限制共同沉淀。** 这些原则同样适用于其他代码智能、性能分析和 Agent 工程项目。

## 10. 2026-10-27：设备发现与采集链路专项复盘

### 10.1 影响与不足

用户多次遇到设备误选、所有设备离线、Bridge 在线却没有手机列表、作业期限到达但结果未知。反复重启和更新两端延长了定位时间。问题不全在手机、驱动或 HDC；平台把设备操作放在跨主机协议上，却没有从一开始以完整任务生命周期验证慢网络、历史积压和多设备状态。

早期修复分别覆盖设备选择、心跳与单条命令，未及时把“请求创建—领取—执行—结果回传—确认—页面投影”串成一条可验证链。1.4.8 修复了心跳阻塞，仍遗漏历史结果对实时结果的队首阻塞。这是测试场景和观测边界的缺口，不能归因于用户没有更新或没有重复尝试。

### 10.2 版本化修复链

| 版本 / 提交 | 核实问题与修复 | 证据入口 |
|---|---|---|
| 平台 0.1.21 / `8404b47` | 任务绑定完整连接路径与设备标识，提交后的任务使用该时刻快照 | `backend/scenario_capture_target.py`、`test_scenario_capture_target.py` |
| 平台 0.1.22 / `78ed9c5` | Windows 本地执行采集包，完整采集绕过逐条远程 HDC RPC，标准归档导入平台 | `backend/scenario_capture_local_package.py`、`test_local_capture_package.py` |
| 平台 0.1.23 / `30d1b7f` | 采集页与助手共用设备选择；无手机时仍可选择路径；不把超时当空列表 | `CaptureDevicePicker.tsx`、`scenario-capture-device.ts`、浏览器回归 |
| 平台 0.1.24、Bridge 1.4.8 / `75825a6` | HDC 诊断、事件发送和 poll 独立；重连 receipt 批量处理，避免反复读写大 registry | `test_device_bridge_http_workers.py`、`test_device_bridge_http_liveness.py`、Gateway 回归 |
| 平台 0.1.25、Bridge 1.4.9 / `79f1859` | 历史 spool 回放与实时结果独立发送；精确 ACK、队列满时保留实时容量、结果确认落盘；页面区分 Bridge 与手机列表状态 | 同上测试中的历史阻塞、慢 ACK、完整真实 HTTP 发现链路 |

1.4.9 修复后用户确认能够发现设备。该反馈验证本次症状恢复，不能推导为所有手机/网络的完整采集和性能建模已验收。

### 10.3 根因分层

1. **状态所有权分散**：页面、助手、连接注册表和任务分别持有部分目标，造成选中设备与执行设备不一致。当前以完整目标和提交快照约束。
2. **前台与后台共享等待链**：诊断阻塞心跳；历史结果阻塞实时结果。健康状态与实时延迟必须独立验证，不能只看“Bridge 在线”。
3. **历史规模没有进入常规回归**：重连对账的整表 JSON I/O 与串行 ACK 在空历史测试中表现正常，历史积累后暴露延迟放大。回归必须带有历史数据和受控慢网络。
4. **错误语义过粗**：未确认结果、设备断开、HDC 失败和成功空列表曾集中表现为“未发现”。当前页面保留分路径诊断和不确定状态。
5. **远程往返放大准备流程**：屏幕、PMU、租约等多次检查叠加跨主机往返，单个命令很短也会使完整任务耗时很长。已有精简检查和本地采集包，下一步将本地设备工作作为桌面能力。
6. **文档滞后**：早期 Web Spec 仍写“不建设采集”，总体文档仍把跨主机部署列为目标，复盘缺少 10 月问题。此次同步修正导航、边界与版本，保留历史统计来源。

### 10.4 不能重复的修复方式

- 不把“延长超时”作为所有阶段的共同补丁；先测命令执行、队列等待和确认耗时。
- 不靠删除历史 spool 或重新注册身份恢复当前设备；先修优先级与对账。
- 不以 Bridge 在线替代设备发现结果，不以 timeout 替代确定失败。
- 不让 Agent 修复设备选择契约；租约与目标绑定由平台执行层完成。
- 不只验证函数桩和无历史 happy path；真实 HTTP 传输、历史 ACK 阻塞、重连和多目标都进入回归。

### 10.5 架构调整与后续门禁

新增 [桌面工作台原型](desktop_workbench_design.md)，将交互式 Agent、HDC 和采集原始文件放在用户 PC，通过 SSH 终端/工具访问远端源码和构建。该方案减少设备交互路径中的网络队列和状态副本；SSH、进程恢复与 Windows 原生模块仍需独立验证，EXE 本身不能保证可靠性。

后续需要补齐端到端时间线、队列深度和结果年龄的统一诊断；建立 Windows + 内部 Agent + 实际服务器的发布验收。此前真实 HTTP 回归覆盖了旧版本故障，但不替代目标 Windows、公司网络和手机固件差异的验证。当前目标是减少执行跳数、明确状态和保存可复查事实，而不是再给每个阶段增加更多检查。
