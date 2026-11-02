# 用户目录部署与运维

默认安装流程见 [轻量部署指南](../DEPLOYMENT.md)。本文件补充运行边界、安全取舍和可选能力，不要求系统目录、root、systemd 或 Nginx。

`deploy/linux/heterogeneous-*.service` 与 `nginx.conf.example` 只是旧的系统托管参考，`heteroctl` 不读取、不复制也不安装它们；默认部署只使用当前用户目录。

## 拓扑

```text
Windows browser ── trusted LAN/VPN ── Linux Next.js :4032
                                      │ same-origin /v1
                                      ▼
                                FastAPI :8006
                                  ├── Kimi Server :58627（自动安装、按需启动）
                                  ├── Build Worker
                                  └── runtime-root/state

Windows Device Bridge ── SSH/VPN API channel ── FastAPI Device Gateway
         │
         └── hdc.exe ── USB phone
```

默认 `--remote` 模式只把统一 Web 入口监听在 `0.0.0.0:4032`；FastAPI 与 Kimi Server 保持回环监听。普通浏览器 API 与 Windows Device Bridge 的 HTTP poll/event 请求都经 Next.js 同源代理访问，因此不需要额外暴露 `8006` 或部署反向代理。旧 `/v1/device-bridges/ws` Upgrade 只作兼容。`--local` 模式会把 Web 入口也限制到回环地址。使用 `heteroctl install|restart --web-port PORT` 可以为当前 runtime 持久化其他 Web 端口。

Windows HDC Device Bridge 在当前单用户可信部署中采用零交互登记，不需要从页面生成或复制配对码：

```powershell
.\deploy\windows\run-device-bridge.ps1 http://linux-host:4032
```

脚本会自动定位 `PATH` 中的 HDC、自举用户目录环境、生成并复用本机身份、发现设备并常驻轮询；后续启动不再需要参数。该身份只用于任务签名、幂等与设备绑定。可信内网可使用 HTTP，跨不可信网络应由 VPN、SSH 隧道或外部 HTTPS 网关保护。

## 进程模型

`deploy/linux/heteroctl` 管理两个顶层进程，FastAPI 再托管一个子进程：

- Next.js production server
- 单 worker FastAPI
- FastAPI Agent Engine 按需启动和回收 Kimi Server

FastAPI 必须保持 `--workers 1`；当前单用户 Session、Bridge 任务队列和运行状态不跨 worker 分片。

日志和 PID 均位于 runtime root。脚本提供 `init`、`install`、`start`、`stop`、`restart`、`status`、`logs`、`paths`、`sdk`、`pmu`、`runtime-libs` 和 `preflight`，不注册开机服务。Kimi 默认通过 `curl -k` 获取官方 `install.sh`，并在执行前把脚本内的 `curl` 命令全部改为 `curl -k`，再安装到 runtime。每个操作开始时显示完整 CLI，持续超过 60 秒则显示操作耗时并续报。需要开机自启时，使用当前账户已有的任务调度设施调用 `heteroctl start`，不要改变平台目录布局。

需要真机构建时，推荐先启动平台，再在“代码仓”页面登记完整 OpenHarmony checkout，并在“真机构建 SDK”中选择该代码仓生成运行资产。产品接口只提交 `source_connector_id`；后端负责解析代码仓当前路径，并持久化 SDK 来源代码仓、源码快照与 manifest hash。刷新页面不会丢失当前分析代码仓或 SDK 来源选择。

无人值守安装或首次启动前预置 SDK 时，管理员仍可使用命令行兜底：

```bash
./deploy/linux/heteroctl install --source-root /path/to/openharmony
./deploy/linux/heteroctl preflight --require-device
```

安装器只提取固定清单中的 musl、GPU、NPU 和媒体头文件到 runtime `assets/openharmony-sdk`，不复制完整源码仓。已安装环境也可用 `heteroctl sdk --source-root ...` 单独补齐；该参数属于部署工具入口，不是页面产品契约。

若目标 AArch64 OpenHarmony `.so` 已集中在一个管理员目录，推荐先导出
`HETERO_LLVM_BIN_ROOT=/path/to/llvm/bin` 和
`HETERO_RUNTIME_LIBRARY_ROOT=/path/to/libraries`，再执行 `heteroctl restart`。
`--llvm-bin-root`、`--library-root` 与 `runtime-libs --library-root ...` 仅作为兼容入口。配置过程只将目录和逐文件
哈希登记到 `state/build-worker/runtime-library-source.json`；所有动态 profile 共用物理
目录，但仍按 profile 白名单选择链接库。平台不再要求创建或复制
`profile-runtime-libraries/<profile>`。

外部工具和路径统一使用环境变量作为主配置面。`init/install/start/restart` 会读取、
校验并持久化当前命令环境中的非空 `HETERO_*` 资源覆盖值到 runtime
`platform.env`，不要求 `--refresh-env`；`start/restart` 会让运行中的后端应用变化。
完整变量清单和优先级见 [部署指南](../DEPLOYMENT.md#71-linux-build-worker-资源环境)。

## 安全边界

- 当前没有用户认证，只能用于可信内网、VPN 或 SSH 隧道。
- Windows Device Bridge 无人工认证或配对步骤；内部机器身份和设备租约只负责防串线、并发互斥与任务完整性。
- Kimi Server、FastAPI 不对局域网直接监听。
- 模型供应商 URL、模型和 Key 由用户在网页设置并保存在当前浏览器；Key 只作为后端短期内存租约进入 Kimi 子进程，不写入 Kimi 配置、平台 artifact 或日志。
- Kimi Agent 是后端执行主体，可直接使用原生 Bash、Read、Edit、Glob、Grep、服务器工具链和连接在服务器上的 HDC。页面交互以及连接在 Windows 客户端的 HDC 通过 MCP / Device Bridge 跨边界执行。
- 平台提交每个 Kimi prompt 时使用 `all_available_except_disabled`：禁用集合精确为 `WebSearch` 与 `FetchURL`，不为后端 Coding Agent 建立额外 allowlist。`heteroctl preflight` 检查有效 Prompt 策略；每次 `heteroctl start` 还会读取 Agent Engine health，确认 `Bash/Read/Edit/Glob/Grep` 至少可用且网络搜索/抓取关闭，策略不一致时拒绝启动完成。
- 轻量模式将 `HETERO_BUILD_REQUIRE_NETWORK_SANDBOX=0`，避免安装系统 Bubblewrap wrapper。Kimi 仍需遵守自身权限、安全过滤和工作区策略；平台不会用低能力 MCP 替代其后端 Coding Agent 能力。

如果机器已经提供当前用户可执行的 Bubblewrap，可以在 `platform.env` 中启用：

```ini
HETERO_BWRAP_BIN="/home/your-user/.local/bin/bwrap"
HETERO_BUILD_REQUIRE_NETWORK_SANDBOX="1"
HETERO_BUILD_SANDBOX_PREFIX="/home/your-user/work/heterogeneous-enable-web/deploy/linux/hetero-build-sandbox"
HETERO_BUILD_SANDBOX_NETWORK_ISOLATED="1"
```

这只引用已有用户文件，不安装系统 wrapper。运行 `./deploy/linux/heteroctl preflight --require-device` 验证配置。

## 运行资产

`HETERO_RUNTIME_ASSET_ROOT` 保存运行资产；PMU 采集器由平台从仓库自动准备，其余工具按部署配置提供：

| 能力 | 资源 |
| --- | --- |
| Trace 团队 JSON | 无 |
| 原始 `.htrace` | Linux TraceStreamer |
| 真机 C/C++ 构建 | Linux LLVM `clang/clang++/ld.lld` |
| Vulkan shader | Linux glslang |
| GPU/NPU/媒体头文件 | 紧凑 OpenHarmony SDK bundle |
| 动态 profile | 审核并哈希固定的运行库 bundle |
| 场景 PMU 采集 | 仓库自带 AArch64 PMU sampler bundle，正常部署自动准备 |

PMU 采集器默认自动准备到 `<runtime>/assets/pmu-sampler`。
真机部署的 `install/start/restart` 均校验并复用已有有效 bundle，缺失或失效时从
`tools/scenario_capture/prebuilt/aarch64-linux-ohos` 原子准备；**无需手动传包、
配置 SDK 或额外执行 PMU 命令**。已部署环境更新后的首次启动同样生效，重复启动
保持幂等。该步骤不联网、不连接设备，分析模式跳过。
`HETERO_PMU_SAMPLER_BUNDLE_ROOT` 可选，用于自定义目标目录。
仓库资产校验失败会明确提示，核心安装/启动继续；`preflight --require-pmu`
可单独严格校验。恢复完整仓库后正常启动会再次自动准备。

维护者修改采集器源码时，使用完整 native SDK 运行
`python -m tools.scenario_capture.build_pmu_sampler --native-sdk /path/to/native --output tools/scenario_capture/prebuilt/aarch64-linux-ohos`，
将源码与更新后的完整预置包一起提交即可。显式配置
`HETERO_OPENHARMONY_NATIVE_SDK_ROOT` 后执行 `heteroctl pmu` 仍支持本机构建；
该 SDK 与 `HETERO_OPENHARMONY_SDK_ROOT` 的紧凑头文件包不同，常规部署不使用它。
设备默认采集器缺失时，采集准备阶段通过本地 HDC 或现有 Windows Bridge 自动
下发并核验；无需手动复制到手机或更新 Bridge。设备权限和事件支持另行探测。

LLVM 使用
`export HETERO_LLVM_BIN_ROOT=/path/to/llvm/bin` 后执行
`heteroctl install|restart` 即可持久化到当前 runtime；该目录必须包含可执行的
`clang`、`clang++`、`ld.lld`。配置变化会重启
FastAPI/Build Worker，`heteroctl status` 会显示后端实际使用的路径。

远程真机部署由平台自动完成工件中转：Linux 构建产物经短期下载授权流式进入 Windows Bridge spool，校验 SHA-256 后由 Windows 本机 `hdc.exe file send` 下发，最后在手机端再次校验。各阶段进入 Agent Run 实时进度，用户和 Agent 都不需要手工复制文件或传递 Linux 路径。

完整 OpenHarmony checkout、历史 Trace 和 30GB 产品代码仓不进入平台安装目录。代码仓通过 Source Connector 指向用户已有目录，或由页面克隆到 `HETERO_STATE_ROOT/managed-clones`。

## 数据与升级

Windows 桌面 App 可从源码独立更新：准备 Git 和 Node.js 24+ x64，退出旧 App，在仓库根目录执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update`。首次取到入口前先 `git pull --ff-only`。脚本缓存依赖与 Python，本地重建和自检后启动；共用原 AppData，不经过 Device Bridge，也不更新旧安装版的快捷方式。只需 EXE 时使用 `-Package` 在本机打包。完整说明见 [desktop/README.md](../desktop/README.md#从源码运行与构建)。

仓库 `data/parallel-modeling/` 随源码部署，包含图库全事件组 PMU 的解析归档与 `raw/gallery-edit-all-pmu-20261023.zip` 原始数据包。更新后可在「并行建模」的「负载归档」中首次载入历史样例，之后直接选择该归档，即可演示负载特征与原始 Trace，无需额外传文件或连接设备；操作路径见 [README](../README.md)。源码发布包须保留该目录及 `raw/` 子目录，启动会校验版本化文件的 SHA-256。

- 持久化备份 `HETERO_STATE_ROOT`。
- `assets` 可从固定来源重新生成，不与 Git 混放。
- Linux 平台与 Windows Device Bridge 使用同一个发布 tag。
- 更新前执行 `stop`；替换源码后必须先运行 `install --skip-deps` 重新生成并校验 production build，再运行 `start` 和 `doctor`。依赖清单变化时不要跳过 Web 依赖安装。
- 生产服务只接受经过完整性校验的 `.next`；启动检查会验证首页引用的每个 JavaScript/CSS 静态资源及其 MIME 类型。
- 页面克隆 OpenHarmony 前仍执行 80GB 空闲空间门禁；runtime root 应位于容量足够的用户可写文件系统。

## 可选 HTTPS

可信内网或 VPN 直连是默认方案。需要更严格隔离时使用 `--local` 加 SSH 隧道。组织已经提供用户可用的 HTTPS 网关时，可以把 `/` 转发到 Next.js、`/v1` 转发到 FastAPI，并关闭 `/v1` 响应缓冲以保留 SSE。证书和网关由外部基础设施管理，不属于本平台轻量安装步骤。
