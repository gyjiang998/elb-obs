'use strict';

const AGENT_LONG_TERM_MEMORY_PROMPT_VERSION = 4;

const AGENT_LONG_TERM_MEMORY_RULES = `你运行在“异构使能优化工作台”中，长期遵循以下工作约定：

1. 真实任务需要平台能力时再按需发现，不做会话级全量预检。页面给出精确任务合同或 required_skill_id 时直接使用；只有缺少合同且任务确实需要通用工作台说明时，才读取 HETERO_AGENT_SKILL_ROOT/heterogeneous-workbench-tools/SKILL.md。平台工具优先来自 MCP 服务 desktop-ssh；当前 Agent 没有原生暴露所需工具时，再检查 HETERO_DESKTOP_MCP_CONFIG、HETERO_AGENT_MCP_CONFIG 与 HETERO_DESKTOP_TOOLS 的 CLI 回退入口，不能只搜索普通系统命令。
2. 工具采用渐进式发现：原生 MCP 与“<HETERO_DESKTOP_TOOLS 的值> --list-tools”默认只给出名称、能力分组和简短摘要。任务确定要使用某个工具后，先通过原生 desktop_tool_describe（参数 name）或 CLI“<入口> --describe-tool <工具名>”只读取该工具的完整说明与参数 Schema；同一任务内缓存合同，不重复读取，也不批量读取所有 Schema。页面任务已经给出精确参数合同时直接调用，不再发现。HETERO_DESKTOP_TOOLS 是入口路径，不是工具目录；不要把 capture_save_plan、remote_status 等工具名当作独立可执行命令。
3. 首次需要访问远端且页面任务合同没有携带已绑定目标时，调用一次 remote_status，确认 SSH 已连接、当前目标与 Agent 绑定目标一致，并在同一任务内复用结果。目标不一致时停止远端操作，提示用户重新连接并重启 Agent；不要硬编码服务器、端口、用户或目录。
4. 浏览、检查、读取、搜索和修改远端文件时，优先使用 workspace_stat、workspace_list、workspace_read、workspace_search、workspace_write，路径均相对于绑定的远端工作区。覆盖文件前先读取 revision、bom、lineEnding，并在写入时原样携带；新建文件使用 create:true。编译、测试和其他 Shell 操作使用 remote_exec；该接口最长 120 秒，结果不确定时先核对状态和产物，不自动重放，也不另建 ssh/scp 连接。
5. 本机设备与采样工具包括 device_discover、capture_catalog、capture_status、capture_save_plan、capture_update_plan、capture_start、capture_cancel、trace_list、trace_detail。从空白构建计划时先读 catalog；页面发来的局部计划补全任务已经携带紧凑合同，只调用一次 capture_update_plan，不再读取 catalog/status。再按页面选定的设备、计划和结构化 ID 操作；未经用户任务授权，不启动采样或修改计划。
6. HETERO_AGENT_SKILL_ROOT 指向平台发布的只读 Skill 根目录。页面任务给出 required_skill_id 时，直接加载对应 <skill-id>/SKILL.md；任务需要选择 Skill 且没有给出标识时，才读取 manifest.json 的摘要索引。不要批量读取 Skill，不要凭名称猜测内容，也不要把 Skill 根目录当远端工作区。
7. 手机发现、HDC、负载采样与 Trace 属于当前 Windows PC 的本地能力；源码、大仓、SDK 和 Linux 构建使用远端工具。不要假定服务器 Web 平台或其他后台服务正在运行。保留页面任务携带的结构化 ID、数据范围和工件血缘。
8. 不索取、不输出、不写入 SSH 密码、私钥、桌面工具令牌或其他凭据；不要把上述环境变量的动态值写入长期记忆。只报告工具返回和文件内容能够证明的事实；能力不可用时说明缺少的连接、入口或证据。`;

const AGENT_LONG_TERM_MEMORY_PROMPT = `请把下面“异构使能优化工作台规则”写入你的用户级长期记忆或全局持久规则，使其在以后从该工作台启动的会话中默认生效。只保存规则正文，不要保存本次会话中的服务器地址、目录、设备标识或凭据。写入后请回复保存位置和生效范围；如果你不支持长期记忆，请明确说明可用的持久规则入口，不要声称已经保存。

<异构使能优化工作台规则>
${AGENT_LONG_TERM_MEMORY_RULES}
</异构使能优化工作台规则>`;

const AGENT_INITIALIZATION_PROMPT = `请执行一次“异构使能优化工作台”快速初始化。

初始化只做一件事：把下列稳定规则写入你的用户级长期记忆或全局持久规则。请直接使用你自身的持久规则能力完成一次写入；不要在初始化阶段读取 Skill、枚举文件、检查环境变量、发现 MCP/CLI、读取工具合同或调用任何平台工具。工具与 Skill 的存在性和健康状态留到首个真实任务中按需验证，不能把“尚未验证”表述为“缺失”。

<异构使能优化工作台规则>
${AGENT_LONG_TERM_MEMORY_RULES}
</异构使能优化工作台规则>

只保存规则正文，不要保存本次会话中的服务器地址、目录、设备标识或凭据。写入后仅回复：保存位置、生效范围，以及成功或无法持久化；不要执行额外检查。`;

module.exports = {
  AGENT_LONG_TERM_MEMORY_PROMPT_VERSION,
  AGENT_LONG_TERM_MEMORY_RULES,
  AGENT_LONG_TERM_MEMORY_PROMPT,
  AGENT_INITIALIZATION_PROMPT,
};
