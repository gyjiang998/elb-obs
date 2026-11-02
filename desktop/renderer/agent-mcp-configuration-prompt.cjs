'use strict';

const AGENT_MCP_CONFIGURATION_PROMPT_VERSION = 1;

const AGENT_MCP_CONFIGURATION_PROMPT = `请为当前 Code Agent 配置“异构使能优化工作台”提供的原生 MCP，使 desktop-ssh 及其 remote_status、remote_exec、workspace_*、capture_* 等工具直接出现在原生工具列表中，不再依赖 CLI 回退。

已知条件：
- 当前进程由异构使能优化工作台启动。
- HETERO_AGENT_MCP_CONFIG 和 HETERO_DESKTOP_MCP_CONFIG 指向工作台生成的 MCP 描述文件。
- 描述文件中包含名为 desktop-ssh 的 stdio MCP Server。
- HETERO_DESKTOP_TOOLS CLI 已能调用 remote_status，说明工作台 Broker、会话和工具实现可用。
- MCP 子进程需要继承当前 Agent 进程的环境，特别是工作台动态注入的 Broker 地址和会话令牌。

请直接执行以下步骤：

1. 识别当前 Code Agent 的产品、版本、可执行程序及其原生 MCP 配置机制。优先检查本机 --help、内置帮助、配置 Schema、已有配置文件和官方命令，不猜测参数名称。

2. 读取 HETERO_AGENT_MCP_CONFIG 指向的描述文件，确认文件存在且 JSON 有效、包含 mcpServers.desktop-ssh，并且 command、args 和 env 字段完整。只报告检查结论，不输出 Broker Token 等敏感环境变量的值。

3. 按当前 Agent 官方支持的方式完成原生接入，按以下优先级选择：
- 如果支持从外部配置文件加载 MCP，让它在每次启动时读取 HETERO_AGENT_MCP_CONFIG 指向的文件；
- 如果支持 MCP 注册命令，使用官方命令注册 desktop-ssh；
- 如果使用固定的用户级或工作区 MCP 配置文件，把生成描述中的 desktop-ssh 条目合并进去，保留原有 MCP Server，并在修改前创建备份；
- 如果只能通过启动参数加载，确认真实参数名称，并生成可直接填入异构使能优化工作台“启动参数”的 JSON 数组，配置路径使用 {mcp_config} 占位符。

4. 不修改工作台自动生成的 integration/mcp.json 本身，因为工作台启动时会重新生成。应修改当前 Code Agent 的原生配置，或给出需要填写到工作台中的 Agent 启动参数。

5. 让 MCP 子进程继承父 Agent 环境。不要写入或持久化任何令牌、Broker URL 或会话信息，包括 HETERO_DESKTOP_BROKER_TOKEN、HETERO_DESKTOP_BROKER_URL 和 HETERO_AGENT_SESSION_ID 的动态值；这些值应在每次启动时由工作台注入。

6. 配置完成后，通过当前 Agent 的原生 MCP 管理界面或工具列表确认 desktop-ssh 已注册，并确认列表含 desktop_tool_describe 与 remote_status。随后必须通过原生 MCP 调用只读工具 remote_status，再用 desktop_tool_describe 读取 capture_catalog 的单工具合同；不要执行会返回大型模板的 capture_catalog。本轮验证不要使用 HETERO_DESKTOP_TOOLS CLI，以确认原生 MCP 链路真正生效。

7. 如果原生工具只能在 Agent 重启后加载，先完成配置，再明确告诉我需要重启并给出重启后的验证步骤；不把“配置已写入”误报成“原生 MCP 已验证”。

最终按以下格式简要汇报：
- 检测到的 Agent 与版本
- 原生 MCP 加载机制
- 实际修改的配置
- 是否需要重启
- desktop-ssh 注册状态
- 原生 remote_status 验证结果
- 原生渐进式工具发现与 capture_catalog 合同读取结果
- 仍需我在异构优化工作台中填写的启动参数；如果不需要则写“无需”`;

module.exports = {
  AGENT_MCP_CONFIGURATION_PROMPT_VERSION,
  AGENT_MCP_CONFIGURATION_PROMPT,
};
