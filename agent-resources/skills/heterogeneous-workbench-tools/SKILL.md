---
name: heterogeneous-workbench-tools
description: 发现并使用异构使能优化工作台提供的本机采样、Trace、远程工作区和应用流程工具。
---

# 异构使能优化工作台工具

## 工具入口

工作台启动 Code Agent 时，会为当前进程提供三类入口：

- 原生 MCP：配置文件路径位于 `HETERO_DESKTOP_MCP_CONFIG` 和 `HETERO_AGENT_MCP_CONFIG`，服务名为 `desktop-ssh`。Agent 框架支持动态加载 MCP 配置时，使用该文件注册工具。
- 命令行回退：可执行入口位于 `HETERO_DESKTOP_TOOLS`。原生 MCP 工具不可见时，先执行 `<入口> --list-tools` 读取当前版本真实的工具名称、能力分组和简短摘要。确定本轮要使用的工具后，执行 `<入口> --describe-tool <工具名>` 只读取该工具的完整参数合同，再按 `<入口> <工具名> [JSON 参数或 @JSON文件]` 调用。例如，无参数调用 `remote_status`，带参数调用 `workspace_read` 时传入 `{"path":"README.md"}`。
- 托管 Skill：Skill 根目录位于 `HETERO_AGENT_SKILL_ROOT`。需要领域方法时，先查看该目录下的 Skill，不要猜测安装路径。

这些入口只在工作台启动的 Agent 进程及其子进程中有效。不要输出或持久化 broker 地址、token、SSH 密码、私钥等凭据。

## 初始化检查

1. 确认上述环境变量存在，但不打印其中的凭据值。
2. 优先检查原生工具列表是否已经出现 `desktop_tool_describe`、`remote_status`、`capture_catalog` 和 `workspace_read`。列表中的普通工具只带摘要和宽参数入口是正常状态。
3. 原生工具不可见时，使用 `HETERO_DESKTOP_TOOLS --list-tools` 建立摘要索引。通过原生 `desktop_tool_describe` 或 CLI `--describe-tool` 按名称读取 `remote_status` 合同后调用它；只读取 `capture_catalog` 的合同来确认采样能力已登记，初始化时不执行大型 catalog。
4. 列出 `HETERO_AGENT_SKILL_ROOT` 下的 Skill，并按任务读取对应 `SKILL.md`。
5. 只把稳定的入口发现规则写入长期记忆。服务器地址、目录、设备序列号和会话状态以每次真实工具返回为准。

## 能力分组

- 远端状态与命令：`remote_status`、`remote_exec`。
- 长驻远程文件：`workspace_stat`、`workspace_list`、`workspace_read`、`workspace_search`、`workspace_write`。
- 本机设备与采样：`device_discover`、`capture_catalog`、`capture_status`、`capture_save_plan`、`capture_update_plan`、`capture_start`、`capture_cancel`。
- Trace：`trace_list`、`trace_detail`。
- 应用流程：`application_flow_*`。执行流程重建时继续读取 `application-execution-flow-reconstruction/SKILL.md`。
- Agent 运行记录：`desktop_agent_run_list`、`desktop_agent_run_read`。
- 自动恢复握手：`desktop_recovery_complete`。

## 使用约定

- 默认目录只用于选工具。实际调用前按名称读取这一个工具的完整合同；同一任务中缓存已读合同，不重复读取，不批量读取所有 Schema。页面任务已经携带精确参数合同的，直接调用目标工具，不再读取摘要目录或合同。`--list-tools --full` 仅供兼容诊断，不用于日常 Agent 任务。

- 访问服务器前调用 `remote_status`，以返回的绑定目标和工作目录为准。目标不一致时停止远端操作并提示用户重新连接、重启 Agent。
- 浏览、读取、搜索和修改远端文件优先使用 `workspace_*`；编译、测试等 shell 工作使用 `remote_exec`。
- 覆盖文件前使用 `workspace_read` 取得 `revision`、`bom`、`lineEnding`，再由 `workspace_write` 原样携带；新建文件使用 `create:true`。
- 从空白构建采样计划时调用 `capture_catalog`，按返回合同生成结构化计划，再用 `capture_save_plan` 保存。页面发来的局部计划补全任务已经携带紧凑合同，直接调用一次 `capture_update_plan`；不要再读取 catalog、status、Trace、远端工作区或全量工具目录。执行采样时沿用页面已选设备和返回的 `planId`。
- 页面任务携带的计划、Trace、流程工件和分析 ID 是本轮血缘，后续调用保持原值。
- 工具超时、断线或结果不确定时，先查询状态和产物，不重复执行可能已经产生副作用的调用。
- 类型化自动恢复任务只在任务携带的授权和冻结目标内完成修复与最小验证。不要调用 `capture_start` 重放原失败步骤；完成后调用一次 `desktop_recovery_complete`，由工作台按冻结参数重试。无法完成时以 `outcome: "unable"` 结束请求。
