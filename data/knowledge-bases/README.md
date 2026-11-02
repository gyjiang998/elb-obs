# Packaged Knowledge Bases

该目录是在线平台唯一读取的版本化知识资产：

- `optimization/`：正式优化知识库，含严格异构实现与 CPU/software fallback 案例。
- `hardware/`：硬件单元、NPU/GPU/DSP/媒体 API 与运行时验证约束。
- `patterns/`：AST 特征确定性匹配所需的最小分层规则。它不包含历史扫描批次和 pair discovery 中间产物。

这些 JSON 属于 Git 版本的一部分，运行时只读。模型输出、机会报告、设备证据和用户 Trace 必须写入 `HETERO_STATE_ROOT`，不得写回知识库。

