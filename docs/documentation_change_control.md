# 文档与实现同步规范

> 状态：`Normative v1.0`
> 目的：约束代码、数据契约、Skill、知识库、MCP/Action、页面和部署文档同步演进，避免“功能已经改变，但下一次开发仍按旧文档修改”的回归。
> 入口：[Spec 索引](spec_index.md)、[跨 Spec 契约](cross_spec_contract.md)。

## 1. 文档层级

| 层级 | 作用 | 冲突处理 |
|---|---|---|
| 权威专项 Spec | 定义产品边界、状态机、schema、不变量与验收门禁 | 预期行为以 Spec 为准；代码不一致时必须在同一变更中修代码或明确更新 Spec |
| 跨 Spec 契约 | 定义 artifact、质量、证据、Action、恢复和兼容性共同规则 | 专项 Spec 不得降低共同契约 |
| 总体技术与关键技术 | 解释系统分层、Why/How、当前成熟度与演进路线 | 不替代接口和状态机定义 |
| README / DEPLOYMENT | 给出当前版本可执行的安装、启动、配置和验证步骤 | 命令必须能在发布闭包内运行，不得依赖父目录或维护者私有环境 |
| 研究与历史文档 | 保存探索过程、旧算法和离线证据 | 不作为当前实现入口；必须显式指向现行 Spec |

`docs/spec_index.md` 是现行规范的唯一导航入口。父目录 `docs_ffrt/` 只保留研究历史，不复制一份会独立漂移的当前规范。

## 2. 变更影响矩阵

| 修改对象 | 同一变更必须检查的文档与资产 |
|---|---|
| Trace 输入、热点或源码归因 | Trace Spec、跨 Spec 契约、Web Spec、artifact schema、真实 `.htrace` fixture |
| AST、词法扫描或代码范围 | 机会识别 Spec、算法方法论、关键技术、正负/变形 fixture |
| 优化或硬件/API 知识库 | 对应 KB README/Spec、manifest/schema、检索 Skill、评测集、总体技术文档 |
| 自建 API 扫描器 | 自建 API Spec、Skill 演进规范、MCP/Action 摘要、页面质量状态、合成测试与真实仓 shadow scan |
| Skill、Prompt 或上下文装配 | Skill 演进规范、Skill 版本/回滚记录、fixture、Token/证据边界和发布说明 |
| Agent Engine、MCP 或 Action | Runtime Spec、跨 Spec Action 契约、Web Spec、前端 descriptor、执行 Trace 与恢复测试 |
| Build profile、模板或真机流程 | 真机验证 Spec、Runtime/Device Bridge Spec、模板 manifest、部署文档和设备回归 |
| 端口、监听地址、安装或运行资产 | `README.md`、`DEPLOYMENT.md`、`deploy/README.md`、预检命令和远程拓扑说明 |
| 桌面宿主、本地 Agent、SSH 或 HDC 执行位置 | `desktop_workbench_design.md`、Agent 接入指南、相关采集/Runtime Spec、桌面使用说明与构建依赖；分别记录开发机测试、Windows 打包、Windows 实机及内部 Agent 验证 |

## 3. 不可退化的不变量

1. **候选发现与模型解释分层**：确定性扫描器决定进入上下文的事实，模型 Skill 负责解释和编排。Prompt 不能找回扫描器未产出的 API、AST 或证据。
2. **公开合同优先**：API 知识库以消费方可见函数、算子/能力项和接口合同为主体；内部 Service/Manager/Builder、示例和调用点只能作补充证据。
3. **操作状态与质量状态分离**：HTTP 成功、任务完成和 JSON 落盘不等于语义质量通过；下游必须消费独立 `quality.status` 及其计数和原因。
4. **模型结论与设备事实分离**：路由、API 推荐和代码生成是模型假设；设备探测、编译、上传、运行、等价性和性能收益分别由真实 action 结果证明。
5. **合成与真实回归并存**：合成 fixture 验证通用规则和负例，代表性真实仓/真机验证规模与集成行为。真实样本中的符号可作为 oracle，不得写成生产特判。
6. **版本可回滚**：排序、配额、schema、Prompt、Skill、模板、profile 或证据语义发生行为变化时必须提升相应版本，并保留上一稳定版本或迁移说明。

## 4. 修改流程

1. 先确定受影响的权威 Spec 和不可变量，再修改实现；探索性 Spike 必须标为 experimental。
2. 同步更新 producer/Skill/schema/profile 版本，以及 artifact 中可追溯的版本字段。
   平台每轮交付更新默认递增 `package.json` 的补丁号，使用 `npm version patch --no-git-tag-version` 同步根依赖锁；同一轮重复构建或重启沿用该版本，以 `build_id` 区分。重新构建并重启前后端后，核对页面与 API 的版本和构建信息一致，操作步骤见 `DEPLOYMENT.md` 第 10 节。
3. 增加最小正例、负例和至少一个等价变形样本；修复真实失败时先抽象失效机制，不把单个函数名、目录名或错误文本写成泛化规则。
4. 运行受影响模块测试；跨层修改还要运行完整后端与前端门禁。
5. 更新总体文档中的成熟度、Spec 索引状态和部署命令；拓扑变化时再更新架构图。
6. 在变更说明中记录：改变了什么、为何改变、证据边界、执行过哪些测试、哪些真实环境验证尚未执行。

## 5. 合并前检查

```bash
git diff --check
python3 -m unittest discover -s backend/tests -p 'test_*.py'
npm run lint
npx tsc --noEmit
npm run build
```

还必须人工检查：

- `docs/spec_index.md` 的状态、版本、数量和阅读顺序是否仍准确；
- README/部署文档是否仍只引用仓库部署闭包和运行时可配置路径；
- 新字段是否同时出现在生产者、消费者、UI、MCP/Action 描述、fixture 与文档；
- 页面成功文案是否错误提升了质量、设备支持、功能等价或性能收益；
- 旧文档是否已标为历史，避免形成第二个“权威版本”。

文档修改不能替代测试，测试通过也不能替代契约审阅。两者必须在同一版本中闭环。
