# 轻量用户态部署指南

本平台默认采用单用户、无 root 的部署方式。安装器不调用 `sudo`、系统包管理器、systemd 或 Nginx，也不会在 `/opt`、`/var`、`/etc`、`/usr` 等系统目录中创建、修改或删除内容。

Linux 服务器运行 Web、API、Agent Runtime 和编译服务；Windows PC 通过浏览器访问，并可选运行 Device Bridge 连接 USB 手机。部署默认采用 `--remote` 模式，Windows 浏览器可直接访问 Linux 的 `4032` 端口。

## 1. 文件布局

默认只有两个可整体删除的用户目录：

```text
heterogeneous-enable-web/                 Git 仓库
├── agent-resources/skill-library/         版本化 Skill 与运行时 profile（部署必需）
├── .venv/                                Python 依赖
├── node_modules/                         Web 依赖
├── .next/                                Web production build
└── heterogeneous-enable-web-runtime/     所有可变运行数据
    ├── platform.env                      生成的环境配置，权限 0600
    ├── state/                            Run、Trace、artifact、workspace
    │   ├── kimi-code/                    Kimi 会话、token；不保存模型 API Key
    │   └── kimi-cli-terminals/           WebShell 原始 ANSI 会话日志
    ├── assets/                           Kimi Code、LLVM、SDK、glslang、TraceStreamer
    │   └── kimi-code/bin/kimi            自动安装并校验的 Agent Engine
    ├── cache/                            pip、npm、XDG 缓存
    ├── logs/                             Kimi、API、Web 日志
    ├── run/                              本次部署拥有的 PID 文件
    └── tmp/                              临时文件
```

发布源码包必须保留 `agent-resources/skill-library/profiles` 与 `agent-resources/skill-library/skills`。FastAPI 启动时会校验推荐 profile 引用的每个版本文件；缺失、空文件或越界路径会直接拒绝启动，避免部署后静默回退到另一套 Prompt。

运行目录默认位于仓库内，可通过环境变量放到任意用户可写的数据盘：

```bash
export HETERO_ROOTLESS_RUNTIME_ROOT="$HOME/heterogeneous-enable-web-runtime"
```

`heteroctl` 会拒绝系统目录、仓库根目录以及不安全的路径。不要以 root 身份运行。

## 2. 前置条件

Linux 用户需要预先具备：

- Python 3.11 或更新版本，且支持 `venv` 与 `ensurepip`
- Node.js 20.9 或更新版本及 npm
- curl
- 首次安装依赖时访问 npm、PyPI 所需的网络

Git 是可选能力：从远程仓库安装、页面 Git Clone 和源码快照需要 Git；从已校验的发布归档安装、离线更新以及基础平台启动不要求 Git。模型推理仍按所选模型服务的网络要求运行。

这些基础工具由操作系统、组织软件中心、`pyenv`、`nvm` 等现有用户态方式提供；平台安装器不会修改系统软件包。

当系统的 `python3` 仍指向旧版本时，可以显式选择 3.11+ 解释器。安装器会显示最终选择，并检查仓库内已有 `.venv` 的版本和基础解释器；旧 3.10 环境或与显式选择不一致的环境会在平台停止状态下自动重建：

```bash
export HETERO_PYTHON_BIN="$(command -v python3.11)"
./deploy/linux/heteroctl python-check
```

## 3. 安装

```bash
git clone <repository-url> heterogeneous-enable-web
cd heterogeneous-enable-web
./deploy/linux/heteroctl install --python-bin "$(command -v python3.11)"
./deploy/linux/heteroctl start
```

不带访问模式参数时等价于 `--remote`。安装完成后，在 Windows 打开 `http://<Linux服务器IP>:4032`。

### 3.1 选择部署模式

部署模式与 `--remote` / `--local` 访问方式独立，保存在当前 runtime 的 `platform.env` 中。已有部署及新部署默认采用真机验证模式。

| 模式 | 安装方式 | 资源预检与功能 |
| --- | --- | --- |
| 真机验证（默认） | `./deploy/linux/heteroctl install --with-device-validation` | 安装、启动时检查 TraceStreamer、LLVM、glslang、SDK、目标运行库和本机 HDC；缺项显示配置提示，基础分析仍可使用 |
| 仅分析 | `./deploy/linux/heteroctl install --analysis-only` | 跳过上述资源检查和安装时 SDK 生成；代码、知识库、流程分析和硬件机会推理正常使用；真机验证入口显示切换部署模式的提示 |

仅分析的完整安装与启动：

```bash
./deploy/linux/heteroctl install --analysis-only
./deploy/linux/heteroctl start --remote
```

后续开启真机验证，先按第 7 节配置所需资源，再切换模式：

```bash
./deploy/linux/heteroctl restart --with-device-validation
./deploy/linux/heteroctl preflight --require-device --require-trace
```

切回分析模式使用 `./deploy/linux/heteroctl restart --analysis-only`。无需重新下载依赖或删除 runtime，已有分析记录保留。也可通过 `HETERO_DEPLOYMENT_MODE=analysis` / `device` 配置；命令行参数优先。`heteroctl status` 与 `/v1/deployment` 可查看当前模式。

缺项提示会指出相应环境变量，`--require-device` / `--require-trace` 可在真机模式下执行严格检查。手机连接 Windows 时，HDC 配置在 Windows Device Bridge 上，Linux 缺少 HDC 仅作连接方式提示。运行库按 GPU/NPU/媒体 profile 列出缺项，可先补齐实际使用的 profile；严格的 `--require-device` 则要求所有列出的真机构建资源就绪。

TraceStreamer 也用于离线解析上传的原始 HiTrace。分析模式跳过其部署预检，但已经配置的 TraceStreamer 仍可使用；上传原始 Trace 进行解析时仍需此工具，已有分析 JSON 的导入不依赖它。部署模式通过后端能力检查与页面入口生效，Skill 和任务提示词保持原有内容。

安装过程会：

1. 创建仓库下的 `heterogeneous-enable-web-runtime/`。
2. 生成权限为 `0600` 的 `platform.env`。
3. 在仓库内创建 `.venv`，检测并按需通过 `ensurepip` 引导 `pip`，然后安装 Python 依赖。
4. 使用 `curl -k` 下载 Kimi 官方 `install.sh`，执行前将脚本内的全部 `curl` 命令改为 `curl -k`，把最新版安装到 runtime 内；安装后再按官方 manifest 独立校验 SHA-256。
5. 在仓库内执行 `npm ci` 和 production build。
6. 执行基础部署预检。

安装器会将上述过程显示为五个明确阶段，并在每个具体操作开始前打印物理工作目录和经过 Shell 转义的完整 `CLI`。每个阶段从当前仓库目录启动，避免调用安装器的终端仍停留在已删除旧目录时产生匿名的 `pwd/getcwd` 错误。某个操作连续执行超过 60 秒时，终端会再次打印操作名称、该操作已用时间、进程 PID 和 CLI；此后默认每 60 秒续报一次。例如可以直接区分 `pip install`、Kimi 最新版查询、官方安装器、`npm ci`、Next.js 构建和部署预检，并看到实际 URL 与参数。失败总结会再次显示具体操作和 CLI。`Requirement already satisfied` 只是 Python 依赖输出，不再需要据此猜测后续是否进入 Kimi、`npm ci` 或 Next.js 构建。Kimi 版本查询、安装器和校验清单下载均使用 `curl -k`；下载后的官方安装器也会在执行前把其中的全部 `curl` 命令改写为 `curl -k`。这会跳过 TLS 证书校验，适用于需要 HTTPS 检查代理或自签名证书的部署网络；二进制仍会按下载的官方 manifest 做 SHA-256 一致性校验。Kimi 使用官方下载脚本的下载与进度逻辑，但通过 `KIMI_INSTALL_DIR`、`KIMI_NO_MODIFY_PATH=1` 被限制在 runtime 内且不修改 shell 配置。npm 默认按 `package-lock.json` 中的官方地址下载，并使用 `replace-registry-host=never` 避免用户级 `.npmrc` 把新版本包改写到尚未同步的内部镜像；确认内部镜像包含全部锁定版本时，可在安装命令前显式设置 `HETERO_NPM_REGISTRY`。npm 会显示 tarball HTTP 获取状态，单次下载等待也有明确上限；失败时安装器会打印 Node/npm 路径与版本、实际安装 registry、用户配置 registry、cache、npm 调试日志及末尾诊断。Kimi、Python 与 Web 依赖可分别跳过，便于从已经完成的阶段继续。

提示阈值通常不需要调整。诊断安装器本身时，可通过 `HETERO_INSTALL_SLOW_OPERATION_SECONDS` 修改首次提示阈值，通过 `HETERO_INSTALL_HEARTBEAT_SECONDS` 修改后续提示间隔；两者单位均为秒，默认都是 `60`。

常用选项：

```bash
# 把可变数据放到用户数据盘
./deploy/linux/heteroctl install \
  --runtime-root "$HOME/heterogeneous-enable-web-runtime"

# 已有依赖时只重新生成 production build
./deploy/linux/heteroctl install --skip-deps

# Python 依赖已经完成，只重试 npm 与后续阶段
./deploy/linux/heteroctl install --skip-python-deps

# node_modules 已验证可用，只重试 Python 与后续阶段
./deploy/linux/heteroctl install --skip-web-deps

# 离线环境复用已有 Kimi Code，或只部署确定性功能
./deploy/linux/heteroctl install --skip-kimi

# 仅准备依赖和运行目录，暂不生成 Web build
./deploy/linux/heteroctl install --skip-build

# 明确重新生成 platform.env
./deploy/linux/heteroctl install --refresh-env

# 只允许 Linux 本机浏览器访问
./deploy/linux/heteroctl install --local
```

自定义 runtime root 后，后续命令要继续传入同一路径，或者持久设置 `HETERO_ROOTLESS_RUNTIME_ROOT`。

## 4. Kimi Code 与模型配置

`heteroctl install` 会使用 `curl -k` 自动下载 Kimi 官方安装器，并在执行前把安装脚本中的 `curl` 命令全部改写为 `curl -k`；无需用户手工运行 `curl | bash`，也无需在 Linux 终端运行 `/login`。平台把 `KIMI_INSTALL_DIR` 指向 runtime，并设置 `KIMI_NO_MODIFY_PATH=1`，因此官方安装器不会修改用户的 `PATH` 或 shell rc。默认布局为：

```text
runtime/assets/kimi-code/bin/kimi     只读式运行二进制
runtime/assets/kimi-code/VERSION      已安装版本
runtime/state/kimi-code/              Kimi Server token 与会话状态
```

平台默认访问 Kimi 官方 `https://code.kimi.com/kimi-code/latest`，安装或升级到当时的最新版本。需要复现特定环境时，可显式指定版本并重新安装，例如：

```bash
HETERO_KIMI_VERSION=0.34.0 ./deploy/linux/heteroctl install
```

`heteroctl start` 会先启动 FastAPI；FastAPI 随即主动拉起 loopback Kimi Agent Engine，控制器会等待 Engine 的 `/api/v1/meta` 就绪后才继续启动 Web。用户不需要单独执行 `kimi web`，也不需要维护第二套 Kimi 服务。如果 Kimi 已在后端通过已有配置或 `KIMI_MODEL_*` 环境配置好模型，页面会直接使用它。

页面右侧下半区不是模拟 CLI 的聊天记录，而是 `xterm.js -> Next.js 同源代理 -> FastAPI -> POSIX PTY -> kimi --auto --session <session_id>`。终端输出和 Platform Run 事件都按 sequence cursor 通过可恢复 JSON 短轮询返回；前台页面使用短间隔、后台页面自动降频。请求之间主动释放 HTTP 连接，避免多个标签页的 10 秒长轮询占满浏览器同源连接槽并阻塞终端按键。流式 Route Handler 只作为旧页面兼容通道保留，并会把浏览器刷新或后端重启造成的断流收敛为可重连 EOF。FastAPI 只允许启动平台安装并校验过的 Kimi 可执行文件，不提供任意 login shell 创建接口；进入 Kimi 后，Agent 仍可按 Kimi 自身权限策略使用 Bash、Read、Edit 等原生 Coding 工具。页面按钮会把高层任务命令写入同一个 PTY/Kimi Session，Kimi 再通过 per-Run MCP 操作页面或远程设备。

真实 WebShell 当前要求 Linux 或 macOS 后端提供 POSIX PTY；Windows 作为浏览器和 Device Bridge 客户端不受影响。如果未来把整个后端部署到 Windows，需要增加 ConPTY adapter。生产启动保持单个 FastAPI worker，因为 PTY 进程表与输出缓冲由该 API 进程持有。远程模式默认只暴露 `4032`：Next.js 通过同源代理转发 API 与终端输出，`8006` 与 Kimi 服务继续监听 `127.0.0.1`。`heteroctl` 将 Uvicorn keep-alive 默认设为 75 秒，避免 Next.js 复用已经被 API 回收的空闲连接；当前页面使用立即返回的 cursor poll，旧 SSE 兼容流仍受优雅关闭边界约束。一次短轮询返回只是浏览器传输边界，不是 Agent 或 PTY 重连：后端 PTY 没有空闲超时，可连续运行数天，直至 Agent 自行退出、用户显式重启或后端服务关闭。

浏览器刷新或网络短断不会停止后端 CLI；恢复时使用 sequence cursor 续读保留缓冲。页面把 Agent 活动状态与 PTY 传输状态分开显示：正常轮询轮换保持“PTY 在线”，真实断线明确显示“恢复中/离线”、重试次数和会话保留语义，不再用 Engine 健康绿点冒充终端在线。PTY ID 只是 FastAPI 进程内传输标识，页面会另外持久保存稳定的 `page_session_id -> run_id` 绑定。API 更新或重启后，旧 PTY 返回 `AGENT_CLI_SESSION_NOT_FOUND` 时不得删除 Run 绑定：页面会持续重试，后端也会按 `page_session_id` 找回原 Platform Run，再使用其 Engine binding 恢复原生 Agent Session。旧版本若曾为同一页面误建空白替身 Run，恢复器优先选择已有真实命令、工具或页面动作的 Run；每次恢复写入 `page.session_recovered` 审计事件。原始 ANSI 日志位于 runtime 的 `state/agent-cli-terminals/<terminal_id>/terminal.ansi`，可能包含用户输入、源码和工具输出，应与其他分析工件使用相同的访问控制和保留策略。

普通终端输入只在页面上下文版本变化时刷新 `task.json`；Skill/MCP 与 Adapter 投影也按上下文、Skill 版本和源文件签名增量更新。相同上下文中的连续对话直接写入既有 PTY，不会在每次回车前重建工作区。用户激活新 Skill、切换分析记录或改变任务模式时，签名变化会自动触发下一次热投影。

管理员需要统一后端模型时，可在权限为 `0600` 的 runtime `platform.env` 中提供 Kimi 官方运行时变量，例如 `KIMI_MODEL_BASE_URL`、`KIMI_MODEL_NAME`、`KIMI_MODEL_API_KEY`、`KIMI_MODEL_MAX_CONTEXT_SIZE` 和 `KIMI_MODEL_PROVIDER_TYPE`。这是服务器密钥配置，适合受控单用户部署；个人 Key 默认仍使用下面的浏览器短期租约，不应写入源码仓。

后端没有默认模型，或用户希望覆盖后端模型时，在页面右侧打开“页面助手设置”，依次填写：

1. 模型服务预设，例如 Kimi Coding；也可以选择自定义服务。
2. API Base URL，例如 `https://api.kimi.com/coding/v1` 或可信内网中的 `http://model-host:port/v1`。地址必须是兼容 Kimi Code 的 HTTP/HTTPS OpenAI 接口根地址，不能填写完整的 `/chat/completions` 路径，也不能在 URL 中携带账号、查询参数或片段。
3. 模型名，例如 `kimi-for-coding`。
4. 上下文长度和个人 API Key。
5. 点击“保存”；可再点击“探针”执行一次真实请求。

URL、模型和 Key 保存在当前浏览器 `localStorage`。保存时，FastAPI 会形成默认 30 分钟的进程内租约，并使用官方 `KIMI_MODEL_*` 临时通道重载其托管的 Kimi Engine，使新 URL、模型和 Key 立即生效；页面会在租约到期前自动续租。Key 不写入 `platform.env`、Kimi `config.toml`、Run、artifact、执行 Trace 或服务日志。点击“清除”只撤销浏览器覆盖和后端短期租约：若后端已有模型配置，Engine 自动回落并继续工作；否则 Engine 保持运行并显示“模型待配置”。API 进程重启后，页面会用浏览器中保存的配置重新建立租约。

托管 Kimi Engine 的浏览器覆盖允许上述合法自定义 HTTP/HTTPS Base URL。HTTP 只适合可信本机或内网，因为 Linux 后端到模型服务之间传输的 Key、Prompt 和响应没有 TLS 加密；远程浏览器填写的 `127.0.0.1`/`localhost` 指向 Linux 后端所在机器，不是 Windows 浏览器所在机器。旧兼容接口在显式 `direct_relay` 模式下仍只转发 Kimi Coding 与 Kimi 开放平台的固定官方地址；这条限制不会再误用于 Engine 配置。配置请求遇到服务未就绪、限流或 5xx 时页面会自动恢复；URL 或字段校验产生的 4xx 会停止重复提交，等待用户修改后再次保存。

已有手工 `/login` 或后端环境配置的 Kimi 模型仍可直接使用。用户在页面明确保存另一套模型时，FastAPI 会通过经过 service token 认证的 loopback shutdown 接口停止旧 Engine，再以新的临时模型环境启动；不会静默沿用旧 Key。新部署推荐使用自动安装与浏览器 BYOK 流程。Kimi 的安装与临时模型变量定义可对照 [Kimi Code 官方文档](https://www.kimi.com/code/docs/kimi-code-cli/configuration/env-vars.html)。

## 5. 启停和诊断

```bash
./deploy/linux/heteroctl start
./deploy/linux/heteroctl status
./deploy/linux/heteroctl doctor
./deploy/linux/heteroctl logs
./deploy/linux/heteroctl logs --service api
./deploy/linux/heteroctl stop
```

`doctor` 会同时从 Web 同源入口检查 Agent Engine，并输出 `service`、`model` 和 `credential` 三个状态；还会分别从 FastAPI 直连地址和 Web 同源入口检查 Agent Adapter 目录。远程浏览器配置失败时，可据此区分 Web 代理故障、Adapter 加载故障、Kimi 服务故障和单纯的模型待配置。

页面右上角持续显示平台发布版本、源码 revision 和本次 Web `build_id`。点击版本可比较浏览器构建、FastAPI 启动时加载的构建、API 进程启动时间以及平台 MCP facade 的工具数量与内容摘要。前后端 `build_id` 不一致，或后端没有 `/v1/platform-version`，都表示当前部署混用了旧进程、旧前端资源或旧源码；停止服务后重新执行 `install --skip-deps`、`start`，再新建 Agent PTY Session。也可以直接检查：

```bash
curl http://127.0.0.1:4032/v1/platform-version
```

默认 `stop` 将 `4032`、`8006` 和 `58627`（或 `platform.env` 配置的对应端口）视为本次单用户平台的保留端口。它先停止 PID 文件登记的服务，再对当前用户遗留的端口监听器发送 `TERM`，等待后仍未释放时发送 `KILL`，最后验证端口已经空闲。FastAPI 的 shutdown 会同时向所有页面 PTY 和 Kimi/MCP 进程组发出终止信号，而不是逐个串行等待；若旧 API 曾被强制结束，启停脚本还会回收 `state/kimi-engine/workspaces` 下经 cwd 与进程组双重证明归属的平台 Agent，以及已变成 PID 1 子进程且命令精确匹配平台 MCP 的进程，不会宽泛匹配或终止用户在其他目录启动的 Agent。它不会使用 `sudo`，也不会终止其他用户的进程。仅需停止当前 runtime 明确拥有的进程时使用：

```bash
./deploy/linux/heteroctl stop --owned-only
```

访问模式只有两种：

```bash
# 默认模式：Windows 或其他电脑访问 Linux 上的平台
./deploy/linux/heteroctl restart --remote

# 本地模式：只允许 Linux 本机访问
./deploy/linux/heteroctl restart --local
```

显式模式会保存到当前 runtime 的 `platform.env`。`status` 会显示当前模式和访问地址。
Web 端口也可以持久化配置。新部署默认使用 `4032`；已有部署可在重启时切换端口：

```bash
./deploy/linux/heteroctl restart --web-port 44032
```

也可以在首次安装时使用 `install --web-port 44032`。端口会写入当前 runtime 的 `platform.env`，后续直接执行 `start` 会继续使用该值。`--web-port` 接受 `1-65535`，且不能与 FastAPI 端口相同。
启动器会校验操作系统中的真实监听范围；切换模式时会重启当前 runtime 管理的旧 Web 进程，不会把 `127.0.0.1` 上的旧进程误判为远程模式已经就绪。若端口由其他进程占用，启动会明确失败而不会擅自终止该进程。

`stop` 只停止当前 runtime PID 文件登记的进程，不会按名称杀死其他用户或其他部署的服务。查看平台使用的全部可变路径：

```bash
./deploy/linux/heteroctl paths
```

`install` 会修改 `.next`、`node_modules` 或 `.venv`，因此运行中的受管服务会被明确拒绝安装。安装器也会扫描使用同一代码目录、但由其他 runtime root 启动的旧平台或构建进程。更新时必须先执行 `stop`，避免旧 Next.js 进程读取被新构建覆盖的清单并向浏览器返回 `500`。仅执行 `restart` 不会重新编译前端；若 `app/`、`next.config.ts` 或 npm 清单比当前 `.next` 构建更新，`start` 会拒绝混用新后端与旧前端，并提示执行 `install --skip-deps`。

标准源码更新流程如下。仅当 `package-lock.json` 未变化且现有 `node_modules` 已验证可用时，才能使用较快的 `--skip-deps`：

```bash
./deploy/linux/heteroctl stop
# 在这里替换或解压新的源码包
./deploy/linux/heteroctl install --skip-deps
./deploy/linux/heteroctl start
./deploy/linux/heteroctl doctor
```

依赖清单有变化时，使用 `install --skip-python-deps --skip-kimi`，让安装器重新执行 `npm ci` 和 production build。不要在生产环境使用 `next dev` 代替 `heteroctl start`。生产构建会先保留上一份完整构建，失败时自动回滚，并保留上一代内容寻址静态资源，使部署前已打开的页面仍能完成 chunk 加载。

`start` 和 `doctor` 会读取当前首页，逐项请求其中真实引用的 `/_next/static` JavaScript 与 CSS，并校验 HTTP 状态和 MIME 类型。页面框架存在但样式错乱时，直接运行：

```bash
./deploy/linux/heteroctl doctor
```

若输出 `next-static-asset` 失败，说明问题位于 production build 或 Web/网关静态资源转发，不是 FastAPI 或浏览器模型配置。

`start` 只接受当前 runtime PID 记录能够证明归属的 Web/API 进程。即使旧进程在 `4032` 或 `8006` 上还能返回健康响应，也不会再被静默复用；启动器会报告未受管监听器，要求先确认其代码目录和命令行并停止正确的旧部署。

默认 `--remote` 模式的监听地址如下：

| 服务 | 地址 |
| --- | --- |
| Next.js | `0.0.0.0:4032`（可用 `--web-port` 修改） |
| FastAPI | `127.0.0.1:8006` |
| Kimi Server | `127.0.0.1:58627`，由 FastAPI Agent Engine 管理 |

## 6. Windows 浏览器访问

默认无需 SSH 隧道。在 Linux 查询地址：

```bash
hostname -I
./deploy/linux/heteroctl status
```

然后在 Windows 浏览器打开：

```text
http://<Linux服务器IP>:4032
```

Linux 防火墙或云安全组需要允许 Windows 到所配置 Web 端口（默认 TCP `4032`）的访问。FastAPI 和 Kimi 不对局域网监听，浏览器 API 请求由 Next.js 同源转发。

当前版本没有用户认证，`--remote` 只应用于可信内网或 VPN，不应直接暴露到公网。需要严格限制为本机或希望通过 SSH 访问时，切换为 `--local`：

```bash
./deploy/linux/heteroctl restart --local
```

然后在 Windows PowerShell 建立隧道：

```powershell
ssh -N `
  -L 4032:127.0.0.1:4032 `
  <linux-user>@<linux-host>
```

保持窗口运行，然后访问 `http://127.0.0.1:4032`。自定义端口时同步替换隧道两侧端口。组织已有 HTTPS 网关时也可在平台外部接入，轻量安装本身不安装或修改网关。

## 7. 可选运行资产

平台启动不要求完整 OpenHarmony 仓、TraceStreamer 或设备编译工具链。按实际功能把资产放入 runtime 的 `assets/`：

```text
assets/
├── trace-streamer/trace_streamer_linux
├── llvm/bin/clang
├── llvm/bin/clang++
├── llvm/bin/ld.lld
├── glslang/bin/glslang
└── openharmony-sdk/
```

### 7.1 Linux Build Worker 资源环境

工具链与外部资源统一以环境变量作为主配置面。部署控制器在
`init/install/start/restart` 的入口读取当前命令环境，校验非空覆盖值、规范为绝对路径，
再原子写入所选 runtime 的权限 `0600` `platform.env`。不需要执行
`--refresh-env`；后续命令即使没有再次 `export`，也会继续读取已持久化的值。

| 变量 | 类型 | 用途 |
| --- | --- | --- |
| `HETERO_PYTHON_BIN` | Python 3.11+ 可执行文件或 PATH 命令 | 创建并校验平台 `.venv`；也可用 `--python-bin PATH` 指定 |
| `HETERO_DEPLOYMENT_MODE` | `device` / `analysis` | 真机验证或仅分析部署；对应 `--with-device-validation` / `--analysis-only`，默认 `device` |
| `HETERO_LLVM_BIN_ROOT` | 目录 | Linux 主机上的 `clang`、`clang++`、`ld.lld` |
| `HETERO_GLSLANG_BIN` | 可执行文件 | Vulkan GLSL 到 SPIR-V 编译器 |
| `HETERO_TRACE_STREAMER` | 可执行文件 | 原始 `.htrace` 解析 |
| `HETERO_OPENHARMONY_SDK_ROOT` | 目录 | 紧凑 OpenHarmony 头文件与 musl 模板 |
| `HETERO_OPENHARMONY_NATIVE_SDK_ROOT` | 可选：完整 native SDK 或其父目录 | 仅供维护者显式 `heteroctl pmu` 重编译；普通部署使用仓库预置包，无需配置 |
| `HETERO_PMU_SAMPLER_BUNDLE_ROOT` | PMU bundle 目录（可尚未创建） | 默认 `<runtime>/assets/pmu-sampler`；由正常部署自动准备固定采集器、构建凭据及哈希清单 |
| `HETERO_RUNTIME_LIBRARY_ROOT` | 目录 | 统一的目标 AArch64 OpenHarmony `.so` 来源 |
| `HETERO_BUILD_RUNTIME_LIBRARY_ROOT` | 目录 | 不可变部署直接读取运行库的高级兼容入口 |
| `HETERO_HDC_BIN` | 可执行文件 | 手机直接连接 Linux 时使用的 HDC |
| `HETERO_OPENCODE_BIN` | 可执行路径或 PATH 命令 | 可选 OpenCode Agent CLI |
| `HETERO_AGENT_CLI_CONFIG` | JSON 文件 | 自定义 Agent CLI Adapter 清单 |
| `HETERO_AGENT_CLI_ADAPTER` | Adapter ID | 默认页面 Agent；也可用 `--agent-adapter ID` 持久化设置 |
| `HETERO_DEFAULT_SOURCE_ROOT` | 目录 | 可选默认源码根目录 |
| `HETERO_TRACE_FIXTURE_ROOT` | 目录 | 可选真实 trace fixture 根目录 |
| `HETERO_API_GRACEFUL_SHUTDOWN_SECONDS` | 正整数秒 | API 更新时等待旧连接退出的上限，默认 `6` |
| `HETERO_ASYNC_POLL_MAX_HOLD_MILLISECONDS` | 正整数毫秒 | 旧页面 JSON cursor poll 的兼容等待上限，默认 `250`、最大 `1000`；新页面使用立即返回的短请求 |
| `HETERO_COMPATIBILITY_SSE_MAX_SECONDS` | 正整数秒 | 旧页面 SSE 连接单次存活上限，默认 `5`、最大 `60` |

推荐配置方式：

```bash
export HETERO_ROOTLESS_RUNTIME_ROOT="$HOME/heterogeneous-enable-web-runtime"
export HETERO_PYTHON_BIN="$(command -v python3.11)"
export HETERO_LLVM_BIN_ROOT="$HOME/toolchains/llvm/bin"
export HETERO_GLSLANG_BIN="$HOME/toolchains/glslang"
export HETERO_TRACE_STREAMER="$HOME/toolchains/trace_streamer"
export HETERO_OPENHARMONY_SDK_ROOT="$HOME/openharmony-sdk"
export HETERO_RUNTIME_LIBRARY_ROOT="$HOME/openharmony-runtime-libraries"

./deploy/linux/heteroctl restart
./deploy/linux/heteroctl status
./deploy/linux/heteroctl preflight --require-device
```

自定义 Agent 需要同时登记清单和默认 Adapter。配置后先执行离线预检，再重启平台：

```bash
export HETERO_AGENT_CLI_CONFIG="$HOME/.config/heterogeneous-enable-web/agent-cli-adapters.json"
export HETERO_AGENT_CLI_ADAPTER="team-codeagent"

./deploy/linux/heteroctl agent-check
./deploy/linux/heteroctl restart --remote
```

`agent-check` 会解析完整 JSON、核对默认 ID、检查可执行文件，并列出所有 Adapter 的 `READY/UNAVAILABLE` 状态。`start`、`install`、`preflight` 和 `doctor` 也会检查清单与默认项；内置 Kimi 尚未安装时仍允许核心页面启动，自定义默认 Adapter 不可执行时则直接报告错误。页面设置会显示 manifest 错误；浏览器曾手动选择其他 Adapter 时仍保留该选择，并明确显示它与后端默认项的差异。

有效优先级依次是：本次命令的兼容参数、本次命令环境中的 `HETERO_*` 覆盖值、已有
`platform.env`、runtime 默认路径。`init/install` 会为下一次启动持久化配置；
`start/restart` 会立即应用，资源值发生变化时会重启本 runtime 已拥有的
FastAPI/Build Worker。单独执行 `export` 无法修改已经运行的进程。

`--refresh-env` 只用于迁移或修复时重建整份 runtime 配置，不是应用工具路径的必要步骤；
使用它时应在同一命令环境中重新导出需要保留的自定义资源值。旧的
`--llvm-bin-root` 与 `--library-root` 参数仍可用，但只是兼容入口。

手机连接 Windows PC 时，不要在 Linux 设置 `HETERO_HDC_BIN`；HDC 路径由 Windows
Device Bridge 自己管理，Linux 只负责构建和工件中转。

#### 场景采集的 PMU 采集器

PMU 数据由手机端通用的 `thread_pmu_sampler.c` 采集器产生。代码仓自带
`tools/scenario_capture/prebuilt/aarch64-linux-ohos/` 下经校验的 AArch64
可执行文件、构建凭据及哈希清单；它面向不同应用的线程采集，不绑定图库场景。

**正常部署无需另传 PMU 包、安装 native SDK 或执行额外命令。**
`heteroctl install/start/restart` 在真机部署模式下自动校验目标资产目录：
已有与当前源码匹配的有效 bundle 时直接复用；缺失或失效时，从仓库预置目录
原子准备到 `<runtime>/assets/pmu-sampler`。已有部署更新代码后的首次启动也会
执行此步骤，重复启动保持幂等。此过程不联网、不连接设备、不运行目标程序，
也不会因为残留的 native SDK 配置而重新编译。分析模式不准备 PMU 资产。

源文件、构建凭据、二进制 SHA-256、AArch64 ELF 及 OpenHarmony musl 加载器
均通过后，资产才可用于下发。仓库预置包缺失或损坏时，命令明确报告校验错误；
保留核心页面及独立的 Trace/HiLog 功能，PMU 结果单独报告不可用。
恢复完整代码仓后再次执行正常 `start/restart` 会自动重试。
`preflight --require-pmu` 可用于严格检查，但不是常规部署的额外必做步骤。

`HETERO_PMU_SAMPLER_BUNDLE_ROOT` 可选，用于覆盖默认目标目录。
`HETERO_OPENHARMONY_NATIVE_SDK_ROOT` 仅供需要主动重编译采集器的维护者使用，
与平台紧凑头文件 SDK 不同；设置后显式执行 `heteroctl pmu` 会用该 SDK 构建。
未设置时，`heteroctl pmu` 复用或恢复仓库预置包。普通部署始终优先使用已校验资产。
两个变量均由控制器持久化到当前 runtime。

**维护者更新采集器源码时**，使用本机宿主版本的完整 OpenHarmony native SDK
重新构建预置目录，并将源码、清单、凭据与二进制一起提交：

```bash
python -m tools.scenario_capture.build_pmu_sampler \
  --native-sdk /path/to/openharmony/native \
  --output tools/scenario_capture/prebuilt/aarch64-linux-ohos
```

同一个 AArch64 OpenHarmony 目标产物可由 Mac 或 Linux 宿主 SDK 生成，
部署服务器不需要该 SDK。常规更新通过代码仓同步即可获得新产物。

采集启动前，平台会检查设备采集器。默认路径的采集器缺失时，通过本地 HDC 或
已有 Windows Device Bridge 自动下发经验证的 bundle，检查设备端哈希及能力契约；
已有自定义采集器仍按其能力单独检查。此更新复用现有 Bridge 下发接口，不需要
手动 `hdc file send`，也不要求仅为 PMU 功能重新安装 Windows Bridge。
构建资产就绪不等同于 PMU 计数器可用：设备权限、事件组支持及实际采样间隔在
采集结果中另行诊断。

#### LLVM 工具链

真机 C/C++ 构建发生在 Linux Build Worker，不发生在浏览器或手机上。LLVM 路径必须是
一个 `bin` 目录，并且同时包含当前 Linux 用户可执行的 `clang`、`clang++` 和
`ld.lld`：

```bash
ls -l /path/to/llvm/bin/{clang,clang++,ld.lld}

export HETERO_LLVM_BIN_ROOT=/path/to/llvm/bin
./deploy/linux/heteroctl restart \
  --runtime-root "$HOME/heterogeneous-enable-web-runtime"

./deploy/linux/heteroctl status \
  --runtime-root "$HOME/heterogeneous-enable-web-runtime"
./deploy/linux/heteroctl preflight \
  --runtime-root "$HOME/heterogeneous-enable-web-runtime" \
  --require-device
```

部署控制器会校验三个程序并持久化绝对路径。不要把该变量指向 `clang` 文件本身、
OpenHarmony 源码根目录或手机目录。

### 7.2 OpenHarmony SDK 头文件

产品主路径不要求在命令行固定源码路径：进入页面“代码仓”，登记并选择完整 OpenHarmony checkout，然后在“真机构建 SDK”中选择该代码仓并点击“生成 SDK”。平台只向后端提交稳定的 `source_connector_id`，由后端解析当前路径、原子生成 bundle，并持久化所选 Connector；刷新页面后当前分析代码仓和 SDK 来源都会恢复。

如果代码仓位置发生变化，在页面重新登记新位置并重新选择即可，不需要修改 `platform.env`。生成内容包含 Build Worker 所需的 musl NDK include、AArch64/generic 模板及异构 API headers。

无网页环境的首次安装仍可用管理员兜底命令：

```bash
./deploy/linux/heteroctl install \
  --source-root "$HOME/source/openharmony"
```

平台已经安装完成时，可只补齐或原子更新 SDK：

```bash
./deploy/linux/heteroctl sdk \
  --source-root "$HOME/source/openharmony"

./deploy/linux/heteroctl preflight --require-device
```

若使用自定义 runtime root，同样设置 `HETERO_ROOTLESS_RUNTIME_ROOT` 或传入 `--runtime-root`。工具只读取打包清单中的头文件目录，并把结果写到 `assets/openharmony-sdk/`，不会复制完整 30GB 代码仓。`--require-device` 会校验打包与构建实际使用的是同一份完整清单；若缺少 `alltypes.h.in` 等 musl 模板，它会在 Agent 写实验源码之前失败并给出修复命令。

### 7.3 目标侧运行库

动态 profile 还需要目标侧 AArch64 OpenHarmony 运行库。若部署环境已经把
`libEGL.so`、`libGLESv3.so`、`libvulkan.so`、NNRT、媒体和 `libc.so`
等库集中在同一个目录，只登记一次该目录：

```bash
export HETERO_RUNTIME_LIBRARY_ROOT="$HOME/openharmony-runtime-libraries"
./deploy/linux/heteroctl restart \
  --runtime-root "$HOME/heterogeneous-enable-web-runtime"
```

LLVM 与运行库也可以在一次启动中共同声明：

```bash
export HETERO_LLVM_BIN_ROOT="$HOME/toolchains/llvm/bin"
export HETERO_RUNTIME_LIBRARY_ROOT="$HOME/openharmony-runtime-libraries"
./deploy/linux/heteroctl start \
  --runtime-root "$HOME/heterogeneous-enable-web-runtime"
```

对于 `HETERO_RUNTIME_LIBRARY_ROOT`，`start/restart` 只在
`runtime/state/build-worker/runtime-library-source.json` 保存绝对路径、文件哈希和各
profile 的 ready/missing 状态，不复制 `.so`，也不创建
`profile-runtime-libraries/gpu_gles3` 等目录。GPU、NPU、媒体 profile 继续按各自固定
白名单选取库，不能因为共享物理目录而链接未声明文件。库目录更新后重新执行同一命令，
审核并刷新哈希；登记阶段会拒绝 x86 ELF，要求文件为 ELF64 AArch64。若只想热更新
运行库登记而不重启服务，仍可执行兼容命令
`heteroctl runtime-libs --library-root <PATH>`；下一次 `build.describe_profile` 或构建会
读取新配置。

不可写 runtime state 的不可变部署也可在 `platform.env` 设置
`HETERO_BUILD_RUNTIME_LIBRARY_ROOT=/absolute/path`。这种方式每次构建现场计算内容哈希；
一般部署优先使用上述登记命令。旧版按 profile 复制的 bundle 只作为兼容读取路径保留，
不再推荐新建。

大体积资产也可在准备机生成后复制到目标 Linux 用户的 `assets/`，再执行预检。不要把运行资产提交到 Git。

## 8. Windows Device Bridge

手机连接 Windows PC 时，Linux 不需要安装 HDC。可信内网/VPN 中，Windows Bridge 直接连接与浏览器相同的 Web 地址（默认 `4032`）：

```powershell
git clone <repository-url> C:\work\heterogeneous-enable-web
cd C:\work\heterogeneous-enable-web

.\deploy\windows\run-device-bridge.ps1 http://<Linux服务器IP>:4032
```

这一个常驻命令会从 `PATH` 自动定位 `hdc.exe`，在 `%LOCALAPPDATA%\HeterogeneousDeviceBridge` 按需创建 Python venv，生成随机 Bridge 身份并注册，然后持续发现当前或后来接入的设备、连接平台并在断线后自动重连。首次运行后，平台地址、HDC 绝对路径和机器身份会被复用，后续只需执行 `.\deploy\windows\run-device-bridge.ps1`。Bridge 主动连接平台，不要求 Linux 服务器访问 USB，也不开放 Windows 入站端口；平台请求固定直连该地址，不继承 Windows 系统或进程的 HTTP 代理。当前可信单用户模式不需要页面配对码。Bridge 与平台应使用相同的 Git tag。

如果 HDC 不在 `PATH`，首次启动可追加 `-HdcPath C:\OpenHarmony\toolchains\hdc.exe`；脚本会把解析后的绝对路径写入 `%LOCALAPPDATA%\HeterogeneousDeviceBridge\bridge.json`。旧的 `install-device-bridge.ps1` 仍作为显式安装入口保留，但普通使用不再需要先执行它。遇到问题时可运行以下诊断确认 Bridge 配置和完整传输链路：

```powershell
.\deploy\windows\test-device-bridge.ps1
.\deploy\windows\test-device-bridge.ps1 -TransferCheck
```

第一条检查平台 HTTP 与 HTTP poll 协议、Bridge/HDC 版本、`hdc list targets` 与 spool；第二条额外执行 Windows 到手机的临时文件发送、设备端 SHA-256 校验和清理。结构化输出可直接用于排查，平台中的 Agent 也会从 `device.discover` 和失败结果取得同源的 `bridge_diagnostics`、`failure_domain`、`stage` 与 HDC 原始结果。Web 与 Bridge 共用外部 `4032`；轮询与事件回传均经同源 HTTP 代理进入回环 FastAPI，不需要对 Windows 开放 `8006`。

平台 `0.1.25` 对应 Bridge `1.4.9`：当前任务与历史回执独立回传，避免慢网络中历史补传使新 `device.discover` 超过 10 秒。Windows 更新仓库代码并重启常驻脚本后生效；页面显示 Bridge 在线但手机列表超时时，核对启动日志版本并查看“设备发现诊断”。

平台 `0.1.24` 对应 Bridge `1.4.8`：HTTP 心跳与 HDC 诊断、历史回执回传分别执行，重连对账批量更新历史回执。更新时在 Windows 先按 `Ctrl+C` 停止旧进程，更新仓库代码，再运行 `run-device-bridge.ps1`；控制台应显示 `implementation_version=1.4.8`。

平台版本更新后，Windows 侧也要替换为同一 tag 的源码并重启 `run-device-bridge.ps1`；Python 依赖未变化时无需重建 venv 或删除 Bridge 身份。Bridge 上报 `implementation_version` 和 `deployment_contract_version`，因此 `device.list_bridges` 可用于确认当前连接的是否仍是旧进程。不要只更新 Linux 后端而长期运行旧的 Windows Bridge。

不希望直接开放局域网端口时，可让 Linux 使用 `--local`，并在 Windows 建立 Web 端口隧道，再把 `-PlatformUrl` 改成 `http://127.0.0.1:4032`：

```powershell
ssh -N `
  -L 4032:127.0.0.1:4032 `
  <linux-user>@<linux-host>
```

真机部署不需要人工复制二进制。`device.deploy_artifact` 会执行固定链路：Linux Build Worker 产物 -> 短期授权的分块 HTTP 下载 -> Windows Bridge spool -> SHA-256 校验 -> `hdc.exe file send` -> 手机端 SHA-256 校验。下载、HDC 下发和手机端校验阶段会进入同一 Agent Run 的实时分析进度；Windows Bridge 不接收 Linux 文件路径，也不暴露 Windows 通用 Shell。

只有返回 `deployment_verified=true`、`remote_path` 非空且 `remote_sha256` 等于构建产物哈希时，部署才算完成。失败返回中的 `requested_remote_path` 只是计划位置，不能直接执行。若同一产物此前已部署、随后被清理，应使用新的 `retry_nonce` 发起真实重传；平台不会把旧成功记录冒充为当前手机文件。Windows Bridge 计算手机目录时始终使用 POSIX 路径语义，不受 Windows 反斜杠规则影响。

## 9. 部署预检

```bash
# Web/API/KB 等基础能力
./deploy/linux/heteroctl preflight

# 额外要求原始 .htrace 分析资产
./deploy/linux/heteroctl preflight --require-trace

# 额外要求真机构建资产
./deploy/linux/heteroctl preflight --require-device
```

`--require-device` 会检查 LLVM、紧凑 OpenHarmony SDK、glslang 等实际资源。未使用某项能力时，不应因为该可选资产缺失阻塞基础平台启动。

基础预检还会校验 Kimi 的有效 Prompt 工具策略：`native_tool_mode=all_available_except_disabled`，后端 Coding Agent 保留 Kimi 提供的原生能力，同时禁用集合必须精确为 `WebSearch` 与 `FetchURL`。每次 `heteroctl start` 都会读取 `/v1/agent-engine/health` 复核该模式、核心 `Bash/Read/Edit/Glob/Grep` 能力与两个禁用项；不符合时启动失败，而不是带着错误权限继续提供服务。

## 10. 更新、备份与卸载

发布版本以 `package.json` 为准。开发方每轮交付更新时默认递增补丁号，并同步根依赖锁中的版本：

```bash
npm version patch --no-git-tag-version
```

例如本次从 `0.1.0` 升至 `0.1.1`，下一轮为 `0.1.2`。同一版本的调试、重复构建或服务重启不重复升版；每次构建自动生成新的 `build_id`。该命令不创建 Git 提交或标签。部署机器直接使用源码包中的版本，无需再次升版。构建完成后重启 Web 与 API，并刷新页面，展开右上角版本信息确认前后端版本一致。`HETERO_PLATFORM_RELEASE` 可覆盖页面上的发布标签；通常保持未设置，自动使用 `v<package.json 版本>`。

### 10.1 Git 工作树更新

```bash
./deploy/linux/heteroctl stop
git pull --ff-only
./deploy/linux/heteroctl install
./deploy/linux/heteroctl start
```

### 10.2 无 Git 的离线更新

在有发布源码的机器上生成版本化归档和 SHA-256，或使用发布方提供的归档。目标机先校验，再覆盖程序文件；runtime root 不放入归档，也不删除：

```bash
cd /path/to/heterogeneous-enable-web
RUNTIME_ROOT="/path/to/existing/heterogeneous-enable-web-runtime"
UPDATE_ARCHIVE="$HOME/heterogeneous-enable-web-update.tar.gz"

./deploy/linux/heteroctl stop --runtime-root "$RUNTIME_ROOT"
sha256sum -c "${UPDATE_ARCHIVE}.sha256"

# 只备份程序文件；state、assets 和 Kimi 用户目录不会被覆盖。
tar -czf "$HOME/heterogeneous-enable-web-code-backup.tar.gz" \
  backend app deploy package.json package-lock.json

tar -xzf "$UPDATE_ARCHIVE" -C /path/to/heterogeneous-enable-web

# 依赖锁未变化的补丁更新复用现有依赖并重新生成 production build。
./deploy/linux/heteroctl install --skip-deps --runtime-root "$RUNTIME_ROOT"
./deploy/linux/heteroctl start --runtime-root "$RUNTIME_ROOT"
./deploy/linux/heteroctl status --runtime-root "$RUNTIME_ROOT"
curl -fsS http://127.0.0.1:8006/v1/health
```

若发布说明标明 `backend/requirements.txt` 或 `package-lock.json` 已变化，不能使用 `--skip-deps`；目标机需要预置相应 wheel/npm 离线缓存，或在可联网环境完成完整依赖安装。回滚时停服、解压代码备份、重新执行 `install --skip-deps` 并启动。不要把 `.venv`、`node_modules`、`.next` 或 runtime 数据混入跨机器发布包。

### 10.3 无 Git 的完整源码包重装

完整源码归档应只包含版本化程序文件，不包含 `.git`、`.venv`、`node_modules`、`.next`、runtime、Trace、Run 或 Kimi 用户数据。推荐解压到新目录，不在旧代码目录上覆盖：

```bash
OLD_APP="/path/to/current/heterogeneous-enable-web"
NEW_APP="/path/to/new/heterogeneous-enable-web"
RUNTIME_ROOT="$HOME/heterogeneous-enable-web-runtime"
SOURCE_ARCHIVE="$HOME/heterogeneous-enable-web-source.tar.gz"

"$OLD_APP/deploy/linux/heteroctl" stop \
  --runtime-root "$OLD_APP/heterogeneous-enable-web-runtime"

# 默认 runtime 位于旧代码目录时，先迁移到独立用户目录。
mv "$OLD_APP/heterogeneous-enable-web-runtime" "$RUNTIME_ROOT"
cp "$RUNTIME_ROOT/platform.env" "$HOME/platform.env.before-source-reinstall"

mkdir -p "$NEW_APP"
sha256sum -c "${SOURCE_ARCHIVE}.sha256"
tar -xzf "$SOURCE_ARCHIVE" -C "$NEW_APP"

# 新代码目录需要新的 .venv、node_modules 和 production build。
# --refresh-env 只重建运行路径；先前自定义项应从备份中人工合并回来。
"$NEW_APP/deploy/linux/heteroctl" install \
  --runtime-root "$RUNTIME_ROOT" \
  --refresh-env
"$NEW_APP/deploy/linux/heteroctl" start --runtime-root "$RUNTIME_ROOT"
"$NEW_APP/deploy/linux/heteroctl" status --runtime-root "$RUNTIME_ROOT"
curl -fsS http://127.0.0.1:8006/v1/health
```

若 runtime 原本就在独立目录，跳过 `mv`，直接复用该路径。完整重装需要 Python/npm 依赖来源；没有 PyPI/npm 网络时，应由发布方同时提供离线 wheel 与 npm 缓存。旧代码目录保留到健康检查、页面访问和历史分析记录验证完成后再处理。

备份 runtime 的 `state/` 即可保留 Run、Trace、分析记录和审计工件。`assets/` 可从固定来源重新生成。

卸载时先执行 `stop`，再删除 Git 仓库和选定的 runtime root。轻量安装不会在其他系统目录留下 service、配置、日志或状态文件。

## 11. 常见问题

| 现象 | 检查方式 |
| --- | --- |
| 页面打不开 | `heteroctl status`，然后查看 `heteroctl logs --service web` |
| 浏览器资源或 API 返回 500 | 先执行 `heteroctl restart --remote` 并强制刷新，再运行 `heteroctl doctor`；它会检查首页引用的当前 Next.js JS/CSS、同源代理和四个首屏接口，并输出近期 Web/API 错误 |
| 页面长期显示“Agent 目录加载/重试中”或 PTY 未启动 | 执行 `heteroctl doctor`，确认 `agent-adapters-direct` 与 `agent-adapters-via-web` 均为 200；若 `web.log` 出现 `ECONNRESET`、`UND_ERR_SOCKET`、`socket hang up` 或 `failed to pipe response`，先更新到采用异步 cursor poll、有界兼容 SSE 和有界 API 退出的版本，再执行 `heteroctl restart`。重启会并行关闭当前 runtime 的 Agent/MCP 进程，并回收此前强杀遗留的进程组；页面从最后序号恢复，不需要靠反复刷新碰运气 |
| 安装停在 `Requirement already satisfied` | 等待最长 60 秒，安装器会在 `[1/5]` 到 `[5/5]` 阶段中显示当前具体操作及其耗时；也可在另一终端执行 `ps -eo pid,ppid,etime,stat,cmd --forest | grep -E 'heteroctl|pip|npm|next'` 判断实际子进程 |
| 已安装 Python 3.11，但安装器仍使用 3.10 | 执行 `heteroctl python-check --python-bin "$(command -v python3.11)"` 核对选择结果，再以同一参数执行 `heteroctl install`。安装器会自动替换仍绑定 3.10 的仓库 `.venv`；无需修改系统 `python3` 或手工删除 runtime |
| 安装提示 `No module named pip` | 新版安装器会先执行虚拟环境的 `ensurepip` 自动引导。若所选解释器执行 `-m ensurepip --version` 也失败，说明它缺少完整的 venv/ensurepip 组件；补全该 Python（Debian/Ubuntu 通常是匹配版本的 `python3.11-venv`）后重新执行 `heteroctl install`，无需删除 runtime |
| `start` 停在 `api: started` | 新版会每 5 秒显示就绪阶段和最新日志，并在进程提前退出时立即回显最近 40 行；本机探针固定绕过 `HTTP_PROXY`，并使用不扫描 runtime 数据的 `/v1/live`。旧版若日志已显示 `Uvicorn running`，可执行 `curl --noproxy '*' -i http://127.0.0.1:8006/v1/health` 区分代理问题与接口错误 |
| API 返回失败 | 查看 `heteroctl logs --service api` |
| 页面助手不可用 | 检查 `runtime/assets/kimi-code/bin/kimi`、页面模型配置、`heteroctl logs --service kimi` |
| `MODEL_RELAY_PROVIDER_UNSUPPORTED` | 先确认已更新并重建 Web/API。托管 Agent Engine 支持合法自定义 HTTP/HTTPS Base URL；只有显式旧 `direct_relay` 仍限制为官方地址。执行 `heteroctl doctor` 查看实际 Engine 状态 |
| 原始 Trace 不可分析 | 执行 `heteroctl preflight --require-trace` |
| 真机编译缺少 musl 或异构 API 头文件 | 优先在“代码仓”页面选择完整 OpenHarmony checkout，并在“真机构建 SDK”中生成或更新运行资产；无人值守环境可执行 `heteroctl sdk --source-root <openharmony>`。随后运行 `heteroctl preflight --require-device`。不要通过删除标准头文件或手写 libc 声明绕过环境问题 |
| Linux 已构建但 Windows 手机无法收到产物 | 确认页面选择了在线 Device Bridge，Bridge 与平台版本一致，且 Bridge 能访问 Web 地址 `4032`；部署进度应依次出现 `artifact_download_started`、`artifact_download_verified`、`hdc_file_send_started`、`device_deployment_verified` |
| Bridge 同时报 `reserved bits must be 0`、`no close frame received or sent` | 这是旧 WebSocket 链路的帧协议/代理兼容错误，不是 HDC 认证错误。Linux 与 Windows 更新到同一版本后执行 `heteroctl restart --remote`，再重启常驻 Bridge；启动日志应显示 `implementation_version=1.4.3`、`transport=http_poll`，doctor 应返回 `platform_http_poll.protocol_verified=true`。HTTP 轮询不使用 Upgrade、压缩位或 close frame |
| Bridge 显示在线，Windows 已识别手机，但采集设备检测超时 | 更新 Windows Bridge 到 `1.4.9` 并停止旧进程后重启。历史 spool 补传与当前任务原先共用串行 HTTP 结果通道；新版分别回传，当前手机列表无需等待旧结果发送完成。平台 `0.1.25` 可进一步区分“Bridge 在线”和“手机列表读取超时”，保留身份与历史文件 |
| `Device Bridge HTTP poll temporarily unavailable: timed out` 或周期性全离线 | 服务器更新到 `0.1.24`、Windows Bridge 更新到 `1.4.8` 并重启两端。该版本让 HDC 诊断和事件回传独立于心跳，并批量处理历史对账。若仍超时，运行 `test-device-bridge.ps1`，按日志中的 `/poll` 或 `/events` 端点及耗时检查 Windows 到平台 Web 地址的连接；保留现有身份与 spool |
| `Device Bridge HTTP poll temporarily unavailable: HTTP Error 403: Forbidden` | 更新 Linux 与 Windows 到包含 Bridge `1.4.3` 的同一版本并重启两端。新版平台 HTTP 固定直连，不受 `HTTP_PROXY`/`HTTPS_PROXY` 截获；旧 identity 或后端已失效的 spool 回执会自动写入 `quarantined-events.jsonl` 后停止回放。日志会明确显示失败端点和 `BRIDGE_*` 错误码；不需要删除 `bridge.json` 或整个 spool |
| `Device Bridge HTTP poll temporarily unavailable: HTTP Error 410: Gone` | 这是后端已清理旧 job、Windows Bridge 仍在回放完成结果形成的兼容问题。更新并重启 Linux 后端即可先恢复：新端点会用 `200 + event_disposition=stale_ignored` 幂等确认，不再让旧 Bridge 无限重试；随后再把 Windows Bridge 更新到 `1.4.3` 以保留本地隔离审计。无需删除机器身份或整个 spool |
| 页面已显示手机连接，但 `device.probe` 数分钟不结束 | 旧 Bridge 会串行执行十余个属性/路径 HDC 命令，最坏时间超过后端等待上限。更新 Windows Bridge 到 `1.4.3`；新版合并为一次固定只读 Shell 探测，30 秒内成功或返回结构化 HDC 超时，并在页面持续回传探测阶段。Bridge 窗口是前台守护进程，回车本来就不会触发命令，`bridge_alive` 表示仍在工作 |
| 磁盘空间不足 | 将 `HETERO_ROOTLESS_RUNTIME_ROOT` 指向容量充足的用户数据盘 |

更详细的安全边界、资产职责和拓扑说明见 [deploy/README.md](deploy/README.md)。
## Windows 桌面 App（0.1.74）

0.1.32 修复 `.npmrc` 与 Electron 下载进程之间的配置断层；0.1.33 将相同的代理、`noproxy` 和企业 CA 桥接到后续 CPython 准备，并增加 `HETERO_PYTHON_ARCHIVE` / `HETERO_PYTHON_MIRROR`。npm registry、Electron ZIP 与 Python ZIP 是三个独立地址，registry 不映射为后两者。两种外部 archive 都先按固定 SHA-256 验证，离线模式不访问网络且保留原文件。完整步骤见 [源码准备下载排障](desktop/README.md#源码准备下载失败先看失败阶段)。源码更新沿用下方命令；0.1.29 EXE 已包含两套运行时，可继续使用。

0.1.34 允许桌面采集源码运行显式复用电脑上的 CPython 3.11–3.15。Windows 已安装 Python 3.15.2 时，在仓库根目录执行：

```powershell
$env:HETERO_DESKTOP_PYTHON = ((& py -3.15 -B -I -S -c "import json, sys; print(json.dumps(sys.executable, ensure_ascii=True))") | ConvertFrom-Json)
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update
```

启动器以 `-B -I -S -X utf8` 禁止写入字节码、隔离环境，并核对解释器、所需标准库及真实采集闭包；通过后跳过内置 Python 下载。显式配置无效时立即失败，不回退缓存或网络。该入口只适用于桌面源码运行；`-Package`、安装版和便携版始终使用固定内置 CPython 3.13.17，保持发布物自包含。Web/FastAPI 后端继续使用独立 `.venv` 和 `backend/requirements.txt`，这里不声明其第三方依赖支持 Python 3.15。完整合同与恢复内置运行时的方法见[使用电脑上已安装的 Python](desktop/README.md#使用电脑上已安装的-python)。

安装 `desktop/release/Heterogeneous-Workbench-0.1.74-win-x64-setup.exe`，或直接运行便携版 `Heterogeneous-Workbench-0.1.74-win-x64.exe`。旧版配置保留。Windows 包含官方 CPython 3.13.17 embeddable、共享采集 Runner、PMU 预编译二进制及收据；用户只需已有的 Code Agent、HDC 与设备驱动，不需要手动传采样器或部署服务器。完整步骤见 [desktop/README.md](desktop/README.md)。

频繁更新推荐 Windows 源码运行，先准备 Git、Node.js 24+ x64（含 npm），退出旧 App，在仓库根目录操作：

```powershell
git pull --ff-only
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1
# 后续一次完成拉取、构建、自检和启动
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-desktop.ps1 -Update
```

首次下载依赖和 Electron；未设置 `HETERO_DESKTOP_PYTHON` 时还会准备固定 SHA-256 的官方 Python。后续代码更新复用缓存，重建界面及采集闭包。企业网络可通过 npm proxy/CA 与两个专用 mirror 接入，也可分别指定 `HETERO_ELECTRON_ARCHIVE`、`HETERO_PYTHON_ARCHIVE` 做离线准备。`-BuildOnly` 仅构建并自检，`-Package` 本机构建安装版和便携 EXE，可与 `-Update` 合用。源码与安装版共用 AppData，不更新旧 EXE/快捷方式；更新后从脚本启动。`build/prepare-capture.cjs` 复制固定部署闭包并产生文件哈希清单，不引用维护者工作区。Windows CI 用 PowerShell 5.1/7 分别验证外部系统 Python 源码路径与内置 Python 路径，并检查保存/修改计划和生成包完整性。源码锁为 `desktop/package-lock.json`。

0.1.28 修复 `plan-store/artifacts/scenario_capture_plan` 的 Windows `Permission denied`：跳过 Windows 不支持的目录句柄同步，保留文件 fsync、原子替换和实际权限错误。更新后重新保存计划；无需管理员权限或删除历史数据。

0.1.29 的桌面布局更新仍使用同一源码启动/打包流程。右侧助手和下方终端成为跨页面面板，分隔条尺寸、折叠状态保存在 Electron 用户数据内的本地存储；不迁移采集文件或连接配置。顶部“重置布局”仅恢复显示尺寸和面板可见性。

现有服务器部署流程保持不变。桌面采集通过本机 HDC 执行，Windows Bridge 可继续服务旧 Web 场景；单台手机进行采集时不要同时由另一端发起采集任务。Trace 标准归档在现有 Web 平台导入后继续分析。桌面负载特征、硬件模型、寻优校准和并行建模页面已经接入；标准采样归档可直接进入完整分析闭环。
