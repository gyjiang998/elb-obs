# 场景采集与并行建模工作台

本实现将操作计划、设备执行、采集证据和条件建模结果分别保存。场景采集生成可编辑的 Python 自动化脚本并执行选中步骤采集；Trace 管理维护原始文件及副本；负载特征从真实 UI callback 中提取 BuildItem、调度与 PMU 证据；寻优校准与并行建模分别生产模型参数、完成基线回放和条件寻优。操作步骤的外层 marker 只表示采集窗口，不等同于应用 UI callback。

## 连接、设备选择与执行位置（2026-10-27）

- 设备目标是 `{connection_mode, bridge_id, device_serial}`，页面与助手共用同一选择。每次提交保存目标快照，后续切换选择不改变已提交任务，也不静默改选另一台设备。
- 连接路径与手机列表独立选择和显示；尚未发现手机时，仍能选择本机或已登记 Bridge 并刷新当前路径。刷新全部连接用于显式重新发现，不因另一条路径错误丢弃已选目标。
- 本机和 Bridge 发现均只获取当前 target 列表；硬件属性和完整能力不放在发现关键路径。恢复上次选择时先标为 `unconfirmed`；只有本轮成功发现可确认在线，成功空列表和请求超时分别展示。
- Bridge 在线仅表示控制连接存活。Bridge 1.4.9 将实时结果与历史 spool 回放独立发送，避免 target 已发现却迟迟不能被页面接收；具体合同见 [Runtime Spec §11.2.1](platform_agent_runtime_build_device_bridge_spec.md)。
- 平台 0.1.22 起提供 Windows 本地采集包，在 PC 运行同一 Runner 并导入标准归档；没有平台发现结果也可下载，包内没有固定设备时由本机选择唯一 target，多 target 要明确指定。固定绑定的设备不可回退到其他 target。
- 桌面 App 0.1.27 已接入本地计划、设备选择、Trace/HiLog/PMU 采集和 Trace 管理；Web 与桌面共用 `plan_contract.py`、固定 Runner、manifest 和质量语义。桌面新计划 PMU 默认 10 μs，Web 默认保持原值。用户已验证 D0 连接链路，D1 新增采集另验，见 [桌面设计 §9](desktop_workbench_design.md#9-d1-本地采集合同0127)。
- 0.1.28 修复共享计划持久化与包导入中的 Windows 目录同步兼容性：文件 fsync、原子替换保留；仅 POSIX 打开目录并同步，不吞掉真实文件权限错误。源码启动与 Windows CI 均运行无设备的实际 Python 保存计划及生成包校验，见[桌面设计 §10](desktop_workbench_design.md#10-windows-持久化与源码更新0128)。

## 采集计划与执行

`ScenarioCaptureManager(state_root, artifact_store, device_gateway)` 提供同步 `catalog()`、`create_plan(payload)`、`get_plan(id)`、`list_plans(limit=100)`、`generate_script(id)`、`execute(id, ...)`、`get_run(id)`、`list_runs(limit=100)` 和 `discover_devices(bridge_id="", connection_mode="bridge")`。HTTP 与 Agent 动作使用同一管理器。

- 默认模板 `gallery_17_steps_v1` 复用原图库 17 步操作、界面定位与选中窗口采集逻辑，来源及文件哈希保存在 `tools/scenario_capture/provenance.json`。原始 PMU C 源码也随项目保留。
- 默认选中 `step_07`，采集 `trace`、`hilog`、`pmu`。执行策略始终为 `through_last_selected`，不执行最后一个选中步骤之后的操作。
- `capture.preparation_mode="auto"` 从第一步开始准备；`"already_prepared"` 从第一个选中步骤开始。两个选中步骤之间的必要步骤仍执行，只有选中步骤开启采集器。
- 自然语言原文保存在 `natural_language_steps`；不能直接执行的操作保留为 `unresolved`。Agent 需写成受支持的操作与界面定位字段后才能执行，readiness 会列出未解决项。
- 默认未校准的城市页面定位仅在执行范围涉及这些步骤时阻止执行。界面尺寸、目标元素及可用手势在执行前检查。
- `screen_preparation.py` 提供通用采集前准备，由 Runner 在每步业务脚本、步骤 marker 和 Trace/HiLog/PMU 启动前固定执行。图库模板、自定义场景、本机 HDC、Windows Bridge 及下载 Python 共用该入口；`already_prepared` 只跳过前置业务操作，仍核验屏幕。预检识别锁屏、认证界面、USB 连接方式弹窗、桌面和计划配置的目标应用页面；已就绪页面保持原样，未知页面返回核验失败。
- Runner 1.2.15 使用 `screen_preparation/v7`：首次查询电源后，明确灭屏才按 Power，未知状态使用标准唤醒；只有实际发出唤醒命令才等待 1 秒，已就绪页面直接继续。电源查询接口在本轮内缓存支持情况（含 `hidumper -s 3301 -a -a` 兼容路径），不缓存屏幕状态，不把传输失败当成接口不支持。页面仍锁屏或未确认时，提前执行计划目标应用的 `aa start -a <ability> -b <bundle>`，为有效解锁方式预留预算。已可用页面保持原样；成功必须有新 UI 证据，最新电源明确灭屏时不放行。图库 Ability 明确不存在时兼容 `com.huawei.photos.MainAbility`，仅更新运行时 profile 副本。
- 对确认的无密码锁屏，应用拉起及剩余标准/专用唤醒仍无效时尝试慢速上滑：以 1280×2832 屏幕上的 `(638,2400) → (638,500)` 为归一化参考，按实际宽高缩放坐标，目标时长 1.9 秒，按实际距离换算 `uitest` 的 px/s 速度，限制为 200–40000。若仍锁屏，再尝试 Back 和原有两种手势：82%→22% 高度、550 ms，以及 92%→12%、900 ms；每次动作后读取新布局，后续手势前再次核验。Back 使用标准 `uitest uiInput keyEvent Back`，仅明确解析不支持且新布局仍是同一状态时兼容小写形式。每轮观察默认最多 2 次、间隔 0.35 秒，4 秒后不再追加轮询，每个设备 I/O 仍有超时与取消，整个正常准备不设累计期限。密码/PIN/图案界面返回认证提示，无法确认的页面保留现场，不通过 Home 改变业务页面。
- 初次屏幕准备与临采检查均不设累计时间上限。各个唤醒、应用拉起、Back/手势及 UI 核验按有限次数顺序尝试，单次设备 I/O 仍请求最多 8 秒并可取消；不会因多次正常远程往返累计超过 45 秒而终止。布局采用短超时可取消回传。`screen_preparation_timing` 与 `screen_handoff_timing` 保留当前命令、累计耗时和结果，`budget_seconds` / `remaining_seconds` 为 null；单次命令超时、取消及传输状态不确定分别记录。
- 准备就绪即继续，手机自动熄屏时间独立管理。`ScreenAwakeGuard` 在任务内临时覆盖有限熄屏时间，至少为步骤超时 + 180 秒，且不缩短原有更长设置；通过 `power-shell timeout -o` 设置与读回，采集收尾、异常及取消均尝试恢复原 override（原无 override 时 `timeout -r`）。只恢复本任务仍持有的值，外部变更保留；读取原设置失败时保持不变。恢复未确认返回质量告警及操作证据。进程被强制终止或设备失联可能导致有限覆盖值暂留，不能将 override 误认为自动到期撤销。
- 脚本传完后再次核验页面；有已确认的防熄屏保护时先读新布局，正常页面不重复查询电源或固定等待，异常页面再进入唤醒/恢复。保亮剩余时间不足覆盖步骤预算 + 60 秒时才刷新。在 `screen_handoff_timing` 记录过程，将详情放入 `capture_handoffs`，重新提取控件、屏幕尺寸和 PID；动作或 PID 改变则重传脚本并再核验一次，持续变化时停止。核验与恢复都在采集器启动之前；不会在采集中循环点击保活。准备中曾拉起应用时，启动测量标记为 `prelaunched_during_screen_preparation` 并提示不代表冷启动。
- USB 连接方式弹窗在最初布局或解锁期间出现时，执行一次关闭序列：先 Back，若新布局仍显示弹窗，再定位可见 `UsbFunctionSwitchExtAbility` 子树内唯一的 `advanced_dialog_button_0`、类型 `Button`、可点击且启用、文本为“确定”或“OK”的关闭控件，按本次实际 bounds 点击中心。该控件的已核验实现仅结束弹窗；USB 模式选项保持原状。每次动作后重读布局，确认界面可用才进入测量。持续或再次出现的弹窗返回 `SCENARIO_USB_DIALOG` 并保留失败截图；无法识别的控件交由现场证据诊断。
- 锁屏、USB 弹窗或布局读取等失败时，另用共享的 20 秒预算采集电源状态、系统锁屏服务诊断及 `screen_preparation_failed.png`；各次 I/O 继续使用最多 8 秒请求及剩余预算。`screen_state_diagnostics` 保留 `power-shell dump -s`、`hidumper -s 3704 -a '-all'` 的有界输出，`screen_evidence_timing` 记录取证进度。采集器已经开始或任务取消时不截图；取证失败或超时不覆盖原始错误。采集器停止和设备锁清理另计，20 秒只限制失败后的取证，不影响正常准备流程。网页“失败现场”提供查看、下载和折叠的操作诊断，便于无法看到设备的用户远程排障。
- 图库启动步骤的自动准备使用 `gallery_reset.py`：先 `aa force-stop <bundle>` 并确认进程停止，再准备目标起点；非采集的启动步骤可重启后验证“照片 / 相册”主页，残留编辑/确认页通过有限返回恢复。“界面已准备”保持当前业务页面。若启动步骤被选中，停止进程和回桌面在测量前完成，启动动作保留在采集窗口；实际进程停止、提前拉起和启动温度分别记录，不把应用重启称作整机冷启动。
- 每步结束并停止采集器后，按语义控件进行有时限的页面就绪检查，只重读布局，不重放被测动作。锁屏、系统弹窗、无法确认的编辑页面会返回具体原因和恢复建议。

### 目标设备与脚本兼容性

- `profile.screen_size_mode` 默认 `dynamic`，历史计划未携带该字段也按动态模式执行。每次布局读取从 `dumpLayout` 根节点 bounds 获取目标设备尺寸，首次识别及尺寸变化时以最多三次读取确认连续稳定尺寸。`screen_size` 是计划的参考尺寸，不再限制手机型号；仅显式 `fixed` 模式执行严格尺寸校验。
- 屏幕根不得带有明确应用/Ability 身份、窗口类型，或窗口 ID 与窗口根类型/层级的组合；即使此类窗口从 `[0,0]` 开始，也不能将其内容边界当成屏幕。中性显示 ID、单独的 `root` 类型以及子节点的窗口身份不影响正常聚合根识别。
- 语义控件使用当前布局的实际中心坐标；归一化位置、手势、缩放与屏幕区域用本次实际宽高换算，归一化点四舍五入后限定在 `0..宽−1`、`0..高−1` 像素范围。横竖屏或尺寸变化会清除旧像素坐标缓存；无效 bounds 或持续不稳定会返回明确错误，不使用旧设备尺寸猜测。保存的计划保持不可变，实际尺寸写入运行 `profile_effective` 和 `screen_geometry`。尺寸观测发生在动作批次前后，不能保证已执行批次内部从未旋转。
- HiTrace 分类以独立 argv 参数传递，例如 `hitrace --trace_begin --trace_clock mono -b 65536 sched ace app`，不拼接为逗号字符串。执行前通过 `hitrace -l` 筛选当前设备支持的分类，manifest 的 `trace_categories` 保存 `selected`、`unavailable`、`argument_format="space_separated"` 和实际 `begin_argv`。
- 下载 Python 最终调用服务端 runner。服务器上仍出现逗号参数时，应同时核对实际后台服务版本及该批次 `capture.sh`；仅更新浏览器或重新下载旧服务生成的 Python 不会替换后台执行逻辑。当前源码与既有本地归档使用空格，不代表远端运行版本已经更新。

下载的 Python 脚本包含完整 `PLAN` 和平台地址参数，通过平台 HTTP/RPC 创建计划、提交执行及轮询任务。脚本默认自动发现设备并选择连接，唯一设备时只需 `--platform URL`；多个设备通过 `--device-serial SERIAL` 指定。仍支持 `--connection-mode local --device-serial SERIAL` 和 `--connection-mode bridge --bridge-id ID --device-serial SERIAL` 显式路由。

连接方式默认由 `connection_mode="auto"` 识别：先实际查询本机 HDC targets，未发现或本机查询失败时，再探测当前项目在线 Windows Bridge；判断依据是实际设备连接。`local` 通过 `LocalDeviceClient` 复用本机执行器；`bridge` 通过 `GatewayDeviceClient` 使用签名 job 和 lease。catalog 默认值为 auto，HDC 通过 `HETERO_HDC_BIN`、运行时资产或 PATH 查找。历史 HTTP/Runtime action 请求省略 connection_mode 时仍按 bridge 处理；新页面、MCP 和生成脚本均显式使用 auto。

两种方式共用步骤 runner，将 trace、后台 HiLog/PMU 与当前步骤动作组合为一个设备侧 shell 生命周期。准备步骤与采集步骤分别生成脚本。页面进入后自动发现，唯一设备自动选中，多设备仅显示目标选择；定期和窗口重新聚焦时刷新，采集中暂停。保留选中设备的序列号与连接路径，设备失联时提示重连并阻止执行，不自动换成另一个目标。

Runner 1.2.15 不再依赖宿主 300 秒截止来结束卡住的设备脚本。精确登记的 action、Trace heartbeat、PMU probe/sampler、HiLog 和 Trace finisher 都按自然结束或冲刷窗口、TERM、KILL、reap 的顺序有界收尾；未能结束的身份保留在 ledger 并令步骤失败。Trace finisher 的自然冲刷窗口按缓冲区大小从 3 秒起、每额外 64 MiB 增加 1 秒；计划允许的 1 GiB 缓冲区为 18 秒，所有输入仍受 25 秒硬上界约束。整段清理生成上界低于 45 秒，因此宿主 shell 时限为 `step_timeout_seconds + 45`，默认 120 秒步骤最多等待 165 秒。只有失败或设备返回非零时才拉取 `cleanup_phase.txt` 与 `cleanup_events.log`，并把最后一个清理事件加入错误诊断；成功路径不增加额外 HDC 往返。

自动发现返回 devices、selected_device、status、message、diagnostics 与 observed_at。每个候选携带 device_serial、connection_mode、bridge_id、display_name、connection_label。设备唯一且探测完整时 selected_device 有值；多个候选为 selection_required，没有目标为 unavailable，Bridge 探测不完整为 discovery_incomplete。本机查询仅执行一次有 5 秒超时的 targets 命令，Bridge 发现并发有界、每个 RPC 最多 10 秒。明确指定的目标在执行前重新确认，job/run 记录实际使用的连接方式及 requested_connection_mode。

Bridge 自动发现及执行前的固定 Bridge 验证携带签名 `targets_only=true`。Bridge 1.4.5 将 HDC 锁等待和命令执行纳入同一截止时间，只刷新目标列表，诊断复用缓存并标注来源；超时或取消立即回收该发现子进程。页面每 12 秒及窗口重新聚焦时刷新，同一页面合并进行中的请求，20 秒后超时重试，采集期间暂停探测。

历史兼容说明（平台 0.1.7 / Bridge 1.4.7；正常准备累计期限已在 0.1.12 取消）：HDC 排队与命令共用剩余时限，锁获得后再次检查截止时间；短文件回收不再被抬高到 240 秒，接收与上传共用剩余预算。本机 shell/回收的 targets 预检同样纳入单次预算并支持取消。部署时更新并重启服务器；Windows 已运行 `implementation_version=1.4.7` 时，本次准备逻辑更新无需重启 Bridge。旧版 Bridge 需同步更新 Windows 脚本、停止旧进程并重新执行 `deploy/windows/run-device-bridge.ps1`；只更新服务器不能更新 Windows 进程。

采集会话按剩余租期和下一操作耗时续租；有效租约不逐条重建，脚本按实际 UTF-8 大小合并上传分片。采集前通过带 owner 的会话记录回收已确认属于本平台的残留采集器，设备状态与清理证据留存。本地执行器从同一项目、同一设备的终态 `run.json` 恢复可回收 run 集合；Desktop 异常退出造成设备侧 `run.json=running` 时，只接受本次应用启动亲自从 active 恢复为 interrupted、且 Desktop journal、受管输出目录、run metadata、项目、计划、设备 serial 全部一致的 run ID 作为补充回收证据。Trace 开始前写入全局持久 claim 与步骤级 `trace.owner`，均绑定 trace root、run、step、随机会话 token 和设备 boot ID；运行期间独立 heartbeat 每秒刷新 trace marker，降低环形缓冲覆盖一次性 marker 后的误判。会话锁丢失时，优先按全局 claim、步骤 claim、boot、token、允许的 trace root 和当前缓冲 marker 严格对账。Bridge/共享设备任一证据缺失都保留正在运行的 Trace；Desktop 对用户明确选中的本机直连私有设备可在取得设备会话后，以 `hitrace --trace_finish` 保存活动残留并复核 `tracing_on=0`。TraceFS 已为 0 时仍不能直接假定 HiTrace 服务空闲：Runner 1.2.15 以登记身份的有界子进程执行 `hitrace --trace_finish_nodump`，核对退出状态、错误文本和所有可读 TraceFS 根，任一证据失败都阻断新采样。该路径不进入 Bridge/共享设备，不使用进程名批量杀进程，也不直接写 tracefs 状态。

同一进程内同一执行器/serial 在入队时预留，重复请求返回 `SCENARIO_DEVICE_BUSY` / HTTP 409。执行器标识为本机或选定 Bridge，直连不会带入旧 Bridge/lease。设备侧另有带 run owner 的原子目录锁，防止不同服务进程或 lease 同时采集。已登记采集器只按 PID、启动时刻、boot ID 与会话 token 的完整身份清理，不按进程名批量终止；Bridge/共享设备发现归属不明的外部 Trace 时拒绝占用。Desktop 私有设备的活动 Trace 使用 `hitrace --trace_finish`，TraceFS 空闲但服务会话残留时使用 `hitrace --trace_finish_nodump`；只有命令与状态复核都成功才继续，任何清理结果不确定都会保留诊断并阻断新采样。

## PMU 与质量记录

平台 0.1.9 使用“通用线程 PMU 采样器”名称；同一程序按 TID 采集，不包含图库业务逻辑。场景由 `profile.bundle` 指定应用，页面提供包名与 Ability 字段；切换至新建自定义场景时清空图库包名、Ability 及选择器，历史计划保留。当前解析唯一 PID 并采其主线程用户态计数，尚不涵盖其他线程、内核态及完整冷启动。旧 1.1.0 构建包、源码入口和设备路径继续兼容，历史 capability schema 与 marker 名称保留。

平台 0.1.8 先检查配置的设备采样器路径；已存在可执行文件时探测并复用。默认 `/data/local/tmp/thread_pmu_sampler` 缺失时先检查历史 `/data/local/tmp/gallery_thread_pmu_focus`；均缺失时校验平台受管 bundle，经本机 HDC 或 Windows Bridge 下发至 `/data/local/tmp/hetero_tools/pmu/<sha256>/thread_pmu_sampler`，验证远端哈希和能力后使用。自定义路径保留用户管理，既有文件不覆盖，保存的计划不修改。平台 0.1.10 将预编译 bundle 纳入代码仓，正常 `install/start/restart` 自动校验并准备 runtime 资产，服务器无需 SDK 或额外传包。维护者修改采样源码时重建并一起提交预置资产；`npm run build` 本身不编译设备程序。页面的资产状态仅检查服务器文件，执行时在启动采集器前完成设备准备；准备失败时明确记录原因并继续其他采集器。随仓库的 `thread_pmu_sampler.c` 当前为 1.2.0（历史 1.1.0 保留兼容），保留原始来源记录；设备已安装 binary 不假定与此源码相同，每轮记录其身份与能力。默认目标间隔仍为 100 μs，现可配置 10–10000 μs；默认采集时长为 10 秒，采集应用主线程。缺失 binary、权限或 perf 事件不可用时保留可用 trace/HiLog，PMU 状态明确为 `unavailable`、`failed` 或 `invalid`，run/job 返回 `partial`。

启动采样前查询 `--capabilities` 并保存 `pmu_capabilities.json`、`pmu_compatibility.json`。声明的支持范围不含请求间隔时返回 `PMU_PERIOD_UNSUPPORTED`；旧 binary 未提供能力信息且请求小于 20 μs 时返回 `PMU_SAMPLER_UPGRADE_REQUIRED`，保留其他采集器结果。旧 binary 的历史间隔路径继续兼容并标记 `legacy_unverified`。部署 10 μs 需要更新平台代码与设备端采样程序两处；构建与自动安装说明见 [采集器 README](../tools/scenario_capture/README.md)。

10 μs 是请求间隔，不是实际时间精度保证。采样器仍使用绝对时间 `clock_nanosleep` 与普通调度策略，线程唤醒、调度和 perf 读取均会产生延迟；绝对时钟可避免累计漂移，不能消除调度延迟（[Linux 接口说明](https://man7.org/linux/man-pages/man2/clock_nanosleep.2.html)）。应先用短窗口对比 100/50/20/10 μs 的实测中位数/P95、跳点率及无 PMU 基线开销，再选择用于细阶段分析的间隔。平台不自动改写历史计划的周期，不通过忙等或实时优先级强行达到请求值。

Trace 使用 mono 时钟，保留原始 ftrace 的绝对时间戳。每个选中步骤记录 `GALLERY_STEP_*`、`GALLERY_ACTION_*`、`GALLERY_HOLD_*` marker、buffer stats、时钟信息、动作结果和界面快照；marker 写入线程不冒充应用主线程。PMU 校验包含 CSV 读取错误、enabled/running、累计单调性、事件数量、时间有序性、target TID、sample_count 与 metadata；同时将采样时间范围与步骤窗口对照，提前结束或包含读取错误时标为 `partial`，保留可用计数，不宣称覆盖整个步骤。没有 trace 时覆盖状态为 `not_measured`。

Runner 1.2.0 输出 `pmu_capture_validation/v2`（validator 2.0.0）。成功读取且位于首次活动计数之前、enabled/running 与事件计数均为零的样本保留为初始基线；`read_error_rows` 仅统计实际读取错误，异常计数、未运行事件及复用分别计数。至少两个有效活动样本仍是差分可用的必要条件，全部为零不会判为采集成功。退出码 5 且元数据 signal=15 表示脚本结束时的正常停止，其他异常退出单列原因。

事件与计数完整性、时间分辨率分别报告。`sampling_quality` 给出目标间隔、实测间隔中位数/P95/最大值、跳过采样时点和计数器运行比例；跳过采样时点或最大间隔超过请求值两倍时提示分辨率降低。PMU 可显示“已采集”且时间分辨率降低，运行质量 warnings 继续保留该限制，整体 run 仍可为 partial。每步保存 `pmu_validation.json` 及带中文原因/建议的 diagnostics。页面兼容历史 manifest：旧 `read_error_rows` 混合了零基线，显示旧口径提示，不推断真实失败数量，不修改历史原始证据。

## Manifest 与存储

计划写为不可变 `scenario_capture_plan` artifact（producer 1.1.0）。执行记录在 `STATE_ROOT/scenario-capture/runs/<run_id>/run.json`；完成时写不可变 `scenario_capture_manifest` artifact（runner/producer 1.2.15）。运行记录与 Manifest 包含 `pmu_preparation`，对应 `pmu_sampler_preparation/v1`，保留资产凭据、设备部署与能力检查结果；准备失败禁止本次 PMU 启动，保留 Trace/HiLog。Manifest 的 `schema_version` 为 `scenario_capture_manifest/v1`；新增字段为兼容扩展，历史文件不改写。主要字段为：

```json
{
  "schema_version": "scenario_capture_manifest/v1",
  "runner_version": "1.2.15",
  "screen_geometry": {"schema_version": "screen_geometry/v1", "mode": "dynamic", "reference_size": [1280, 2832], "actual_size": [1080, 2400], "source": "dumpLayout.root.bounds", "status": "confirmed", "observations": []},
  "run_id": "capture_<24 hex>",
  "plan_id": "art_<24 hex>",
  "connection_mode": "local",
  "device_access_mode": "private",
  "execution_provenance": {"connection_mode": "local", "executor": "local_hdc", "device_access_mode": "private"},
  "status": "partial",
  "capture_step_ids": ["step_07"],
  "steps": [{
    "step_id": "step_07", "captured": true, "target_pid": 123,
    "collector_status": {"trace": "collected", "hilog": "collected", "pmu": "unavailable"},
    "trace_validation": {"valid": true, "spans": []}
  }],
  "files": [{
    "step_id": "step_07", "kind": "trace", "name": "trace.ftrace",
    "relative_path": "07_edit/trace.ftrace",
    "managed_path": "scenario-capture/runs/capture_<24 hex>/07_edit/trace.ftrace",
    "size_bytes": 123, "sha256": "<64 hex>", "device_job_id": "<local or bridge job>"
  }],
  "quality": {"warnings": [], "errors": []},
  "cleanup": {"status": "completed"}
}
```

`managed_path` 相对 STATE_ROOT，不是浏览器路径。建模导入通过 `capture_run_id` 读取已结束 run，优先读取不可变 manifest，再验证受管文件路径、质量与 SHA-256。运行中的采集不能导入。导入器必须发现完整 UIVsyncTask 与其内的 BuildItem；只有步骤 marker 的文件不能成为有效建模输入。

失败记录在 `quality.errors` 与失败步骤的 `error` 中保留 `code/reason/suggestion/step_id/step_title/phase`，同时提升为 run.error 和 job.error，供页面与 Agent 直接读取。历史记录仍可从原 manifest 中提取失败原因，不改写旧采集证据。

## 异步任务与生命周期

场景采集页新打开时保留历史列表，结果选择为空。浏览器保存的任务指针先向服务端查询，仅恢复仍为 `queued/running` 的采集任务；已结束或不存在的旧任务清除指针，详情由用户点击历史记录查看。当前窗口新建或确认为进行中的任务结束后，自动展示结果并清除对应指针。清理前核对任务 ID，避免删除其他窗口的新任务；恢复响应不得覆盖本窗口的新提交，临时网络错误保留指针并重试。

`ParallelWorkbench` 把同步管理器封装为持久化任务，最多同时执行两个后台任务。任务 JSON 保存于 `STATE_ROOT/parallel-jobs/parallel_job_<24 hex>.json`，记录输入、进度、结果、错误与时间。状态为 `queued`、`running`、`succeeded`、`partial`、`failed`、`cancelled`，后四者为终态。

- 取消排队任务立即结束，不执行设备动作；取消运行任务触发协作取消，并停止该批次的本机 HDC 进程组或 Bridge job，等待清理完成才释放设备预留。本机采集与清理分别使用独立 run token，取消采集后仍可停止采集器、回收证据与释放设备锁。
- `recover_after_restart()` 在应用 startup 显式调用，将未结束的历史任务记录为 `JOB_INTERRUPTED`，不在模块 import 时改写运行记录。不会自动重放设备操作。
- `shutdown(wait=True)` 发出取消信号并等待后台任务收尾。
- `GET /runs/{run_id}` 在尚未进入 runner 时也返回 queued 占位；服务重启中断会反映失败状态。
- `result` 保留完整 run/analysis/search。缺少采集器的 `partial` 不会伪装为全部采集成功。
- Agent 的 catalog、设备发现、run/job 查询与取消结果重新读取，避免相同轮询参数命中旧 checkpoint；计划、分析和采集提交继续幂等复用，防止重复设备执行。

## HTTP / Agent 合同

以下路径均位于 `/v1`。错误使用现有 `PlatformError` 合同。设备发现与执行在 HTTP、Agent 两条路径均执行部署模式检查；直连检查本机 HDC，Bridge 模式检查 Bridge 属于当前 `artifact_store.project_id` 且在线。未知连接方式返回 422，本机未找到 HDC 返回 503。

| 方法与路径 | 输入 / 输出 |
| --- | --- |
| GET `/scenario-capture/catalog` | 模板、默认配置、支持操作、PMU 资产、连接方式、本机 HDC 状态与当前项目 Bridges |
| POST `/scenario-capture/devices/discover` | `{connection_mode:"auto"}` → 统一候选与自动选择结果；显式 local/bridge 保持兼容 |
| POST `/scenario-capture/plans` | `{template_id, steps, natural_language_steps, profile, capture, ...}` → plan |
| GET `/scenario-capture/plans` | `{plans: [...]}` |
| GET `/scenario-capture/plans/{id}` | plan |
| GET `/scenario-capture/plans/{id}/script` | `text/x-python` attachment；Agent `scenario_capture.generate_script` 返回 `{filename, media_type, content, sha256, plan_id}` |
| POST `/scenario-capture/plans/{id}/execute` | `{connection_mode:"auto", device_serial?, bridge_id?}` → `{job_id, run_id, status:"queued", ...}`；唯一设备可省略目标，多个候选需明确选择；显式 local/bridge 保持兼容 |
| GET `/scenario-capture/runs`、`/runs/{id}` | `{runs:[...]}` 或 run |
| POST `/parallel-modeling/datasets/import` | `{capture_run_id, step_id?, target_tid?, target_vsync_id?, hardware_profile_id?}`；也支持归档 builtin、parsed 或 trace_text |
| POST `/parallel-modeling/analyses` | `{dataset_id}` → job |
| POST `/parallel-modeling/searches` | `{analysis_id, space, objective?, ...}` → job |
| GET `/parallel-jobs/{id}` | 最新 job，终态 result 为实际结果 |
| POST `/parallel-jobs/{id}/cancel` | 取消请求后的 job |

## 大文件回收

本机 HDC 直连采用网关的 `file recv` 路径，场景采集传入 2 GiB 的独立上限，使用增量哈希记录文件身份。其他本机设备动作保持 32 MiB 默认上限。

本机回收、采集归档和桌面计划包都使用每次操作唯一的同目录临时名，并以排他发布提交最终文件。相同身份且 SHA-256/长度一致的重试返回幂等复用；相同目标名出现不同字节时拒绝覆盖。Windows Bridge 持久 Trace 若已完成原始文件发布但元数据写入中断，重试会核对完整哈希后补建元数据；不同内容的孤立文件继续保留并报冲突。该合同避免同一手机操作重试或并发回传争用共享 `.partial`、覆盖结果或触发 `rename` 冲突。

回收继续使用 `/v1/device-bridges/jobs/{job_id}/collected` 和既有 Bridge 身份校验。Bridge 从 HDC 接收文件后按块计算 SHA-256，以文件迭代器与明确 Content-Length 上传；不通过 `read_bytes()` 把大文件装入内存。服务端 `request.stream()` 增量计数和哈希，临时文件通过长度、哈希校验后原子落盘为 `jobId-hashPrefix.bin`。失败、超限或连接中断删除临时文件。服务端上限由 `HETERO_MAX_TRACE_UPLOAD_BYTES` 配置，默认 2 GiB；Bridge 回收上限 2 GiB。建模解析另有 256 MiB 单文件导入上限。

## 已验证范围

本轮验证使用模拟 DeviceClient、本机 HTTP 回环服务器和项目内真实归档样本，不操作实际手机。测试覆盖组合采集脚本与失败清理、PMU 缺失、上传超过 128 MiB、增量校验与失败清理、设备冲突、排队/运行取消、重启恢复、脚本下载，以及采集 manifest → HTTP 导入 → 分析 → 寻优。归档样本回放为 86.940 ms；指定大核/中核、3 worker 的条件模型输出约 53.873030 ms，此值是条件预测，尚非应用并行改造后的真机实测。


## 2026-10-16 集成验收记录

- 新增及相关后端测试 64 项通过；MCP、AgentRuntime 的无源码工作流、幂等提交与实时轮询已覆盖。
- 全量后端回归执行 662 项、跳过 2 项；一个现有部署启动测试因 2 秒阈值实际记录 3 秒失败，独立复测通过。外部 `HETERO_TRACE_STREAMER` 环境覆盖会干扰既有部署用例，本轮全量运行移除此测试环境覆盖，部署代码保持原样。
- `npm run lint`、TypeScript 检查及生产构建通过。
- 独立临时状态目录下的真实 Chrome 验证：计划保存、Python 下载、归档导入、关键路径分析、搜索、候选收益展示、预测 DAG 切换均成功，无页面脚本错误。得到 53.873 ms / 38.0% 条件预测，应用 CPU 代价 109.836 core-ms。
- 采集动作采用模拟 DeviceClient 验证；未发起真实手机操作或外部模型对话。最终构建恢复默认 API 代理地址，临时 QA 服务已停止。

## Mac 本机直连验收

- 新增本机执行器、HTTP、Agent/MCP 集成用例，相关后端测试共 86 项通过；覆盖连接方式校验、直连无需 Bridge、设备预留、取消进程与清理、大文件回收、脚本参数和历史 Bridge 路径。
- lint、TypeScript 检查和生产构建通过。真实 Chrome 验证 Mac 默认直连、HDC 可用状态、隐藏 Bridge 选择，以及切换模式后清空旧设备信息。
- 经本地预览的实际设备发现接口识别到 1 台 HDC 设备。本轮实际设备验证限于发现，采集执行与取消使用模拟设备及独立 HDC 子进程桩验证。

## 图库页面准备修复与真机复验

- 复现的两个历史失败布局分别为锁屏/USB 模式弹窗、图库照片浏览页。修复页面状态判断，自动准备支持从照片浏览页返回图库主页；失败位置、中文原因和恢复建议在任务与历史结果中直接显示。
- 新增 12 项页面准备回归用例；与采集、直连、HTTP/Agent 集成相关的 54 项测试通过，lint、TypeScript、生产构建通过。
- 使用原计划 `art_ebd1a112cc5d3bb9bcf75889` 从真实照片浏览页重跑，批次 `capture_8f8c0f454f6b34335d805e44` 的第 1–7 步全部成功，随后结束。Trace 54,052,986 字节、HiLog 939,838 字节已回收；PMU 共 9,876 行，其中 7,007 行满足当前计数可用性检查，2,869 行 running_ns 为 0，CSV 中 read_error 均为 0。当前校验将这些不可用计数行归入不完整结果，因此批次保持 partial，未将其升级为完全采集成功。
- Chrome 复验历史错误详情可见、新批次 Trace/HiLog 为已采集、PMU 为部分采集，可继续导入并行建模。采集进程与设备锁正常清理。

## 自动设备连接验收

- 本机发现、网关、MCP 与 Skill 相关 46 项测试通过；自动路由、场景采集与任务集成 47 项测试通过。后续 Bridge 快速发现与兼容合同、自动路由、任务集成组合 68 项测试通过（各批次有交集）。覆盖唯一/多设备、离线 Bridge、发现不完整、固定目标重验证，以及 HDC 锁等待超时、取消与子进程回收。
- 前端模拟生命周期的 11 个场景、lint、TypeScript 和生产构建通过。真实 Chrome 验证当前手机自动选为本机直连、窗口聚焦刷新、连接区无手动配置；模拟 API 验证多设备选择、断连保留原目标、发现不完整不自动选择和唯一 Bridge 设备自动连接，页面脚本错误为 0。
- 本轮真实设备交互仅查询连接列表，没有重新执行手机采集；Windows Bridge 路由通过模拟 API 与执行器验证，未部署或连接真实 Windows Bridge。

## 2026-10-22 无密码唤醒与 PMU 口径复核

- 已连接设备实测：原图库编辑页保持不动；短暂熄屏后识别到锁屏，显式亮屏并上滑一次后重新核验为原图库编辑页。只执行屏幕准备，没有执行图库保存、退出编辑或完整场景采集。
- 只读复核最近三份真实 PMU 归档。最新批次 `capture_910d4c6848970d85a91e2e16` 的 8,764 条样本实际读取错误为 0，6,112 条活动样本、2,652 条初始零基线；Trace 调度事件也确认零基线发生在目标线程首次运行之前。旧记录中“读取错误 2,652”是旧校验口径，不是实际读失败。
- 该批次请求间隔 100 μs，实测中位数 115.781 μs、P95 413.391 μs、最大间隔 38.207 ms，跳过 9,865 个采样时点。新口径下计数与窗口完整性通过，时间分辨率降级仍被明确保留；没有以该复核改写原记录或声称每个细粒度阶段均可可靠归因。

## 2026-10-22 多设备兼容与 10 μs 可选目标

- HiTrace 独立 argv、动态尺寸、归一化手势、旋转缓存失效、旧计划兼容、PMU 参数与能力探测等定向回归 77 项通过。覆盖探测超时/取消/输出异常及子进程清理；10 μs 不兼容时，实际 shell 回放确认继续保留 Trace/HiLog，且不会再向该 binary 发起时钟查询。
- 全量后端执行 841 项：839 项通过、2 项跳过。前端 lint、TypeScript、生产构建及 6 项 PMU 展示测试通过。浏览器验证 catalog 的 10–10000 和兼容范围 50–5000 μs 动态生效、默认保留 100 μs、无页面脚本错误。
- 使用 1080×2400、1440×3120、2400×1080 布局验证实际坐标；只读回放 33 份既有真机布局通过。没有重新执行手机动作、修改图库或运行实际 PMU 采样。
- 源码 1.1.0 已通过 OpenHarmony SDK 交叉编译为 ARM64 可执行文件，源/二进制 SHA-256 与构建命令随本地 build receipt 保存。尚未安装到远端手机，未实测该手机的 10 μs 达成率与采样开销。
