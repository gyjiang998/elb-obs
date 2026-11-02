# Windows HDC Device Bridge

`hdc_device_bridge.py` 是 Windows PC 上的轻量执行组件，不是 Coding Agent。它默认通过普通出站 HTTP 轮询领取任务和回传事件，校验后端签名的 `device_job/v1`，并以固定 argv 启动管理员配置的 `hdc.exe`。平台 HTTP 固定直连用户配置的局域网/VPN 地址，不继承 Windows 的 `HTTP_PROXY`/`HTTPS_PROXY`，避免公司代理或网关把本地 Bridge 请求误报为 `403/502`。它不会开放入站端口，也不会执行 Windows Shell、PowerShell、`cmd.exe` 或 `shell=True`。WebSocket 仅保留旧客户端兼容，不再是生产默认通道。

## 配置与运行

在连接了手机和 HDC 的 Windows PC 上执行：

```powershell
.\deploy\windows\run-device-bridge.ps1 http://platform.example:4032
```

该命令会从 `PATH` 自动定位 `hdc.exe`，在 `%LOCALAPPDATA%\HeterogeneousDeviceBridge\venv` 按需创建独立 Python 环境，生成随机 Bridge 身份并自动登记，然后保持前台运行。它每 30 秒刷新设备列表，手机后插入或重新连接不需要重启 Bridge；网络中断后也会持续重连。首次启动后可直接运行 `.\deploy\windows\run-device-bridge.ps1`，无需重复参数。配置默认保存在 `%LOCALAPPDATA%\HeterogeneousDeviceBridge\bridge.json`，其中的不可见机器身份只用于绑定和签名任务，不得提交、复制或共享。

如果 HDC 不在 `PATH`，首次运行使用 `-HdcPath C:\OpenHarmony\toolchains\hdc.exe`。原有 `install-device-bridge.ps1` 仍可用于需要固定名称或显式安装的场景，但不是正常启动的前置步骤。

可信内网可以直接使用平台的 HTTP 地址。跨不可信网络部署时仍建议由外部 VPN、SSH 隧道或 HTTPS 网关保护链路。

### 一键诊断

Bridge 会把从 `PATH` 或 `-HdcPath` 解析到的 HDC 绝对路径写入 `bridge.json`，后续稳定复用。配置后可在 Windows 执行：

```powershell
# 检查平台 HTTP、完整 HTTP poll 协议、Bridge 版本、配置中的 hdc.exe、
# hdc -v、hdc list targets 和 Windows spool 可写性。
.\deploy\windows\test-device-bridge.ps1

# 再发送一个随机小文件到 /data/local/tmp，校验设备端 SHA-256 后清理。
.\deploy\windows\test-device-bridge.ps1 -TransferCheck

# 多设备时显式指定 serial。
.\deploy\windows\test-device-bridge.ps1 -TransferCheck -DeviceSerial <serial>
```

诊断结果是结构化 JSON。`failure_domain` 用于区分：

| failure_domain | 含义 |
|---|---|
| `windows_bridge_configuration` | `bridge.json` 中登记的 HDC 绝对路径无效 |
| `windows_spool` | Windows 临时产物目录不可写 |
| `windows_hdc_process` | `hdc.exe` 无法启动或基础命令失败 |
| `windows_hdc_device_connection` | 该 HDC 实例未发现手机 |
| `linux_to_windows_transport` | Linux 构建产物未能下载到 Bridge spool |
| `windows_hdc_to_device_transport` | `hdc file send` 失败 |
| `device_filesystem` | 手机目录创建、权限或落盘失败 |
| `device_integrity` | 手机端文件哈希缺失或不一致 |

页面选择远程 Bridge 后，新任务的 `device.discover` 会把同一份 `bridge_diagnostics` 交给 Agent；后续部署失败也会返回 `stage`、`failure_domain`、HDC invocation、返回码、stdout 和 stderr。这样 Agent 无需访问 Windows 通用 Shell，也能判断故障边界。

`1.4.3` 起，`device.probe` 将固定属性与运行库检查合并为一次只读设备 Shell 调用，超时上限为 30 秒，不再串行执行十余个 20 秒 HDC 命令。探测开始、完成或失败都会回传 `job.progress`。常驻窗口每 60 秒输出一次 `bridge_alive`，设备数量变化时立即输出；该窗口是前台守护进程，不接受回车命令，使用 `Ctrl+C` 停止。

`1.4.5` 起，场景采集页的自动设备发现使用签名 payload 中的 `targets_only=true`，只查询本次 `hdc list targets`，同时返回带缓存来源标记的既有诊断。任务 deadline 覆盖 HDC 锁等待与命令执行，总时限最多 10 秒；超时会回收本次发现的 HDC 子进程。能力字段 `discovery_profiles` 包含 `targets_only`。传统显式发现仍刷新完整诊断。已有 Windows Bridge 更新此脚本后可使用快速发现；旧版本若未能在平台时限内完成探测，页面会保留待确认状态并继续刷新。

`1.4.8` 起，HTTP 轮询持续维持平台心跳，HDC 周期诊断和事件回执各由独立后台任务处理。单次 HDC 检查缓慢或大量历史结果排队时，不再阻塞轮询；回执按顺序确认，未确认结果继续保留在本地 spool。服务端重连对账批量记录回执，避免对每条历史任务重写整个 registry。HTTP 超时会打印请求端点、耗时与时限；`/poll` 指向平台轮询链路，`/events` 指向事件回传链路，二者均不等同于手机已断开。

`1.4.9` 起，当前任务的接收、进度及结果回执，与历史完成结果的后台回放分开发送。历史回放缓慢时，当前设备发现和采集结果仍能及时返回，避免出现 Bridge 在线且已识别手机、采集设备列表却因等待旧回执而为空的情况。已确认结果会记录平台地址与 Bridge 身份并保留在本地，进程重启后的恢复回放保持后台优先级。

`1.4.8` 的心跳及重连对账修复需要同时更新服务器与 Windows。服务器已为平台 `0.1.24` 时，`1.4.9` 的当前结果回传修复只需更新 Windows；平台 `0.1.25` 另提供更准确的设备列表状态提示。Windows 更新后先在旧窗口按 `Ctrl+C`，再从新仓库目录执行：

```powershell
.\deploy\windows\run-device-bridge.ps1
```

启动输出中的 `implementation_version` 应为 `1.4.9`。平台地址变化时再显式传入新地址；已有同地址身份和采集文件继续复用。

## 执行边界

`1.4.6` 增加持久 Trace 文件库：场景采集的 `retain_trace` 请求在上传前将原始文件保存到配置文件旁的 `traces/`（可用 `trace_root` 指定），上传失败也会保留。目录按项目与 Trace ID 隔离，独立于传输 spool。平台「Trace 管理」显示 Windows 路径，通过签名 `trace.library.list` / `trace.library.delete` 查询和删除指定副本；使用者也可从本地工具打开或清理文件，刷新后同步文件状态。更新 Windows 脚本并重启 Bridge 后，此能力才会生效。

`1.4.7` 将 HDC 排队等待与实际执行计入同一超时预算，并用签名 Job deadline 限制整个设备任务。锁排队后再次确认剩余时间，已超时或取消的布局读取、返回键、滑动等命令不会在稍后取得锁时补执行。显式的短文件采集时限（例如 UI 布局或失败截图的 20 秒）按请求执行；未指定时仍保留大 Trace 的 600 秒默认值，文件接收与平台上传共享剩余时间。到期回收本次 HDC 子进程；取消确认等待所属命令退出，保持其他任务的锁和进程。持久 Trace 的删除保护延续到上传线程退出。

使用以上时限修复，需要在 Windows 更新 `device-bridge/hdc_device_bridge.py`，停止旧 Bridge 后重新运行 `deploy/windows/run-device-bridge.ps1`。同平台地址会沿用已有 Bridge 身份和配置。控制台及平台 capability 的 `implementation_version` 应为 `1.4.7`；只更新服务器不会替换 Windows 上正在运行的 Bridge 进程。

- Bridge 支持 `device.discover`、`device.probe`、`device.deploy_artifact`、`device.execute_shell`、`device.collect_artifact`、`device.cancel_job`，以及受管 Trace 文件操作 `trace.library.list` / `trace.library.delete`。
- 每个 job 都含 deadline、Bridge ID、请求 ID、幂等键和 HMAC 签名。它们用于任务完整性与设备防串线，不是用户认证。Bridge 在执行前验证 schema、签名、目标、期限和能力目录。
- 部署前会下载后端短期授权的构建产物并校验 SHA-256；上传后再验证手机侧文件 hash，hash 不一致的产物不会执行。
- 手机目录固定按 POSIX 路径处理；部署只有在 `deployment_verified=true`、设备路径和设备端 SHA-256 均与任务合同一致时才成功。
- 手机侧 Shell 命令是 Agent 在用户授权范围内选择的**设备命令**；Bridge 将它作为单个 `hdc -t <serial> shell <command>` 参数传递，不会解释为 Windows 命令。
- Bridge 本地 spool 保存完成 job 的结果。HTTP 请求失败后会退避重试、发送 reconcile，并回放尚未确认的完成结果；状态未知的写入 job 不会被 Bridge 自行重复执行。旧 identity 的结果，或后端已明确标记为 stale/unauthorized 的历史事件，会写入 `quarantined-events.jsonl` 后停止回放，不能阻塞新轮询和新任务。
- 同一份 `bridge.json` 和 spool 同时只允许一个常驻进程持有；重复启动会明确退出，避免两个进程反复抢占同一机器身份或重复领取任务。
- 取消请求会终止对应 HDC 子进程并保留部分输出。平台保存取消和迟到结果的审计事实，不能因为迟到成功结果而把已取消任务重新标记为成功。

## 更新 Bridge

Linux 平台与 Windows Bridge 必须使用同一发布 tag。平台源码更新后，在 Windows 替换代码并停止旧的 `run-device-bridge.ps1` 进程，再从新目录重新运行该脚本；依赖清单未变化时不需要再次安装 venv。`device.list_bridges` 返回的 capability 中应包含当前 `implementation_version` 与 `deployment_contract_version`。显式重试部署时提供新的 `retry_nonce`，否则相同 job 仍按幂等合同复用已有终态。

平台侧的 Agent Runtime、Build Worker、租约、恢复与证据边界见 [平台 Agent Runtime、后端编译与 HDC Device Bridge Spec](../docs/platform_agent_runtime_build_device_bridge_spec.md)。

## `403` / `410` / 历史回执诊断

`1.4.2` 起，后端已不存在的 job 回执按幂等 no-op 返回 `200`，并标记 `event_disposition=stale_ignored`；Bridge 会将其写入隔离审计后继续工作。这样滚动更新时，即使 Windows 暂时还是旧客户端，也不会因 `410 Gone` 反复重试。跨 Bridge 提交仍返回 `403`，`/poll` 的真实鉴权失败也会持续报告，不会被错误吞掉。若日志只有无结构化平台错误码的 HTML `403`，通常是旧版 Bridge 误走系统代理；更新 Windows 源码并重启常驻 Bridge，无需删除 `bridge.json` 或 spool。
