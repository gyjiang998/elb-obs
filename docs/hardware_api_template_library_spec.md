# 硬件 API 语义族模板库 Spec

> 状态：`Phase A 已实现`  
> 范围：GPU、NPU、DSP 和媒体 API 知识记录到真机实验模板的覆盖、选择、适配、证据与发布合同。  
> 依赖：[real_device_validation_spec.md](real_device_validation_spec.md)、[adaptive_reasoning_and_skill_version_spec.md](adaptive_reasoning_and_skill_version_spec.md)。

## 1. 目标

硬件 API 知识库回答“有哪些接口、语义和约束”，模板库回答“如何避免 Agent 每次从零重写容易出错的 loader、context、资源生命周期和测试协议”。模板不是优化结论，也不是设备支持或收益证据；它是可审计的实现先验。

平台不为 204 条 API 复制 204 份完整程序。合理粒度是：

```text
稳定 Harness
  + API 语义族模板
  + 当前热点 Adapter
  = 本轮最小可证伪实验
```

- **稳定 Harness**：loader/context、设备或 codec 生命周期、错误处理、清理、结果协议。
- **语义族模板**：Vulkan buffer compute、GLES 图像、GLES compute、NNRT 图、AVCodec 生命周期、HCodec/Image Codec HDI、DSP provider 等。
- **热点 Adapter**：shader/算子、shape/format、输入、CPU oracle、比较规则和 benchmark。

## 2. 覆盖等级

| 等级 | 含义 | Agent 可做什么 |
| --- | --- | --- |
| `executable_harness` | 固定 profile 下有完整稳定脚手架 | 适配核心后重新构建、部署和真机验证 |
| `lifecycle_with_adapter` | 公共生命周期可共享，算子或格式仍需补齐 | 生成窄适配器，并逐项验证 capability/输出 |
| `integration_recipe` | 系统、HDI 或 provider 接口，无便携应用直调面 | 定位真实系统构建面；不得伪称可直接下发 |

`catalog.json` 保存 `covered_semantic_families` 和 `coverage_level`。后端回归读取四份硬件 API 知识库，要求当前 204 条记录全部被覆盖；新增未映射语义族会直接使测试失败。

## 3. 当前覆盖

| 单元 | API 记录 | 语义族模板 |
| --- | ---: | --- |
| GPU | 18 | Vulkan compute-buffer、GLES fragment-image、GLES 3.1 compute-SSBO |
| NPU | 146 | 一个 NNRT 图生命周期 Harness + activation/convolution/matrix/reduction 等算子族 Adapter 合同 |
| DSP | 9 | Audio Effect HDI/provider 生命周期与 AP fallback recipe |
| 媒体 | 31 | AVCodec capability、编解码生命周期、HCodec Component HDI、JPEG/HEIF Image Codec recipe |

所有记录都有模板归属，不等于所有记录都有可执行源码。NPU 单算子必须补 tensor/参数 Adapter；HCodec、Image Codec HDI 与 DSP provider 仍受系统构建面、服务和权限约束。

## 4. GPU 成本感知选型

| 维度 | GLES Fragment | GLES 3.1 Compute | Vulkan Compute |
| --- | --- | --- | --- |
| 适合负载 | RGBA、纹理、图像逐像素处理 | 规则 buffer/SSBO 数据并行 | 通用 buffer compute、显式资源合同 |
| 宿主脚手架 | 最少 | 较少 | 最多 |
| 核心调用 | `glDrawArrays` | `glDispatchCompute` | `vkCmdDispatch` |
| 同步与内存控制 | 驱动管理较多 | 中等 | 最显式 |
| 首轮功能证伪 | 通常最低成本 | 低到中 | 较高 |
| 生产迁移价值 | 已有 GL/纹理管线时高 | 轻量 compute 管线 | 已有 Vulkan、零拷贝或复杂同步时高 |

OpenGL ES 宿主代码更短，是因为 EGL/context、shader program 和纹理/SSBO 后，驱动承担了更多状态与同步管理。当前模板中 GLES Fragment、GLES Compute、Vulkan Compute 的宿主代码分别约 249、322、859 行，可作为维护成本的粗略工程量信号，不能当作性能指标。Vulkan 需要显式完成 instance/device/queue、内存、descriptor、pipeline、command buffer、barrier/fence 和清理，因此首轮实验更重，但资源所有权和同步证据也更精确。官方 Vulkan compute 教程同样把 storage buffer、descriptor、command buffer 和 dispatch 作为完整链路；OpenGL ES 3.1 则直接定义 compute shader 与 `glDispatchCompute` 能力。参考：[Khronos OpenGL ES 3.1 Registry](https://registry.khronos.org/OpenGL/specs/es/3.1/)、[Khronos Vulkan Compute Shader Tutorial](https://docs.vulkan.org/tutorial/latest/11_Compute_Shader.html)、[Vulkan Descriptor Sets](https://docs.vulkan.org/tutorial/latest/05_Uniform_buffers/01_Descriptor_pool_and_sets.html)。

本轮在一台 OpenHarmony 真机上的模板晋升也验证了 loader 合同的重要性：GLES Compute 直接链接 EGL/GLES 后在首次 EGL 调用处以 `SIGSEGV` 退出；改为 `dlopen("libEGL.so")`、`dlopen("libGLESv3.so")` 与逐符号 `dlsym` 后，成功创建 OpenGL ES 3.2 context，完成 `glDispatchCompute`、SSBO readback、CPU oracle 比较并以设备返回码 0 结束。该现象只证明这一设备/profile 的部署合同，不应外推为所有 OpenHarmony 设备都必须动态加载。

默认启发式不是硬门禁：

1. 图像/纹理变换先比较 GLES Fragment。
2. 普通规则 buffer 在设备可建 GLES 3.1 context 时，先比较 GLES Compute。
3. 需要显式内存/同步、复杂 buffer、现有 Vulkan 管线或更接近生产合同，再使用 Vulkan。
4. 单一路径初始化失败只否定该路径，不能自动否定 GPU。

## 5. 选择与适配流程

1. 路由阶段先判断硬件单元和数据语义，不逐条展开所有 API。
2. 在所选单元内检索 API Top-N，再按 `covered_semantic_families` 查询模板。
3. 比较 `semantic_match`、exposure layer、profile 和 `coverage_level`。
4. 读取一个精确 `template_id`；平台校验目录与逐文件 SHA-256。
5. 保留稳定 Harness，只替换热点 Adapter；记录复用和修改边界。
6. 重新执行 profile、构建、部署、设备运行、CPU oracle 与端到端 benchmark。

Agent 不得把模板历史 PASS、模板中的示例 kernel 或 capability query 当成本轮热点证据。`integration_recipe` 没有便携 profile 时，应形成可复核的系统集成结论，而不是借其他 profile 伪造卸载。

## 6. 发布与评测

新模板必须声明语义族、暴露层、适配点、覆盖等级、证据等级、限制和逐文件 SHA。至少通过 catalog 完整性、知识库全覆盖、固定 profile 暂存/构建测试和无私有 Run 数据检查。设备通过后只能把该模板提升到相应设备与输入合同范围内的 `device_function_verified`；不能外推到所有设备、格式或热点。

模板命中率、首轮构建成功率、达到设备执行的轮次、功能比较通过率和端到端收益应分别统计。模板盲测继续保留，用于验证模型脱离模板后的泛化能力；生产流程优先复用模板以降低无价值的脚手架试错。
