# OpenHarmony NPU API 知识库

> 自动生成：`python3 docs_ffrt/tools/build_npu_api_knowledge_base.py`。不要手工编辑生成文件。

## 定位与边界

本目录把 NPU 候选识别落到**公开 API 与图算子**。它不把 `OH_NN_OPS_*`、MindSpore delegate 或源码中的 HiAI 符号误写成“当前麒麟设备已经支持”。

- **NNRT 图表达能力**：当前源码的 `OH_NN_OPS_*` 枚举可被 `OH_NNModel_AddOperation` 放入图。
- **源码集成证据**：已有 strict CPU/software fallback 案例或 MindSpore 内部 delegate 使用点可作为相近实现的证据。
- **设备可用性**：必须在目标机上完成设备枚举、完整图的 `OH_NNModel_GetAvailableOperations`、编译和端到端 benchmark；本知识库统一标注为 `evidence_required`。

## 内容统计

- NNRT 图算子：108
- 公开 NPU/NNRT/MindSpore 入口：38
- 关联的严格 NPU fallback 案例：22
- 与公开 NNRT 算子精确映射的严格案例：18；未强行映射的案例：4
- MindSpore 内部 HiAI delegate 符号：67（不是已证实的公开应用 API）

## 推荐的候选识别与验证流程

1. 对热点函数/计算核心提取 AST 特征：循环嵌套、数组/张量访问、dtype、shape、固定权重、归约、数学调用与调用图。
2. 用这些特征检索本目录的 `operator_aliases`、`ast_signals` 和 `expected_data_forms`，得到 Top-N **图算子候选**；语义族词仅作辅助信号，不冒充算子精确命中。
3. 检查热点代码能否收敛成连续张量图，是否有稳定输入边界、权重与 shape，以及转换/拷贝/同步成本。
4. 用下方运行时协议验证实际 device、操作支持、模型构建与端到端收益；任一步失败都保留 CPU/software fallback。

## 运行时验证协议

1. 调用 OH_NNDevice_GetAllDevicesID 枚举当前运行时设备。
2. 对 deviceID 调用 OH_NNDevice_GetType 和 OH_NNDevice_GetName；只把实际返回的 OH_NN_ACCELERATOR 作为专用加速器候选。
3. 用真实输入 shape、dtype、layout、量化参数和完整子图构造模型，并调用 OH_NNModel_Finish。
4. 对目标 deviceID 调用 OH_NNModel_GetAvailableOperations；逐节点 false 即表示该完整图不能直接按当前形状/参数部署。
5. 调用 OH_NNCompilation_SetDevice 与 OH_NNCompilation_Build；失败时保留 CPU/software fallback。
6. 在包含预处理、数据格式转换、buffer 拷贝、同步和回退的端到端 workload 上 benchmark 后，才可给出收益建议。

## 工作台访问方式

- `GET /v1/hardware-kb/npu-api/records?query=`：浏览和按算子/API/语义族/职责检索目录摘要。
- `GET /v1/hardware-kb/npu-api/records/{record_id}`：查看一条记录的完整中文说明、头文件约束、静态匹配信号、构图职责、严格案例关联与运行时门禁。
- `POST /v1/hardware-kb/npu-api/search`：对热点代码执行确定性的 Top-N 图算子候选排序；候选仍不是设备支持或收益结论。

## 公开入口

| API | 层级 | 一句话摘要 | 工作流角色 | 源码位置 |
|---|---|---|---|---|
| `OH_AI_ContextAddDeviceInfo` | MindSpore Lite 上下文/设备 | 向 MindSpore 上下文加入设备配置。 | MindSpore 设备配置 | `openharmony/third_party/mindspore/interfaces/kits/c/context.h:142` |
| `OH_AI_ContextCreate` | MindSpore Lite 上下文/设备 | 创建 MindSpore Lite 上下文，用于组织模型执行和设备配置。 | MindSpore 上下文 | `openharmony/third_party/mindspore/interfaces/kits/c/context.h:56` |
| `OH_AI_CreateNNRTDeviceInfoByName` | MindSpore Lite 上下文/设备 | 按名称创建 MindSpore 的 NNRT 设备描述。 | NNRT 设备选择 | `openharmony/third_party/mindspore/interfaces/kits/c/context.h:286` |
| `OH_AI_CreateNNRTDeviceInfoByType` | MindSpore Lite 上下文/设备 | 按 NNRT 设备类型创建 MindSpore 设备描述。 | NNRT 设备选择 | `openharmony/third_party/mindspore/interfaces/kits/c/context.h:294` |
| `OH_AI_DeviceInfoSetDeviceId` | MindSpore Lite 上下文/设备 | 将具体 NNRT 设备 ID 绑定到 MindSpore 设备配置。 | NNRT 设备绑定 | `openharmony/third_party/mindspore/interfaces/kits/c/context.h:302` |
| `OH_AI_DeviceInfoSetFrequency` | MindSpore Lite 上下文/设备 | 设置设备频率偏好；源码文档限定其仅对 NPU 设备有效。 | NPU 频率偏好 | `openharmony/third_party/mindspore/interfaces/kits/c/context.h:222` |
| `OH_AI_DeviceInfoSetPerformanceMode` | MindSpore Lite 上下文/设备 | 设置 MindSpore 的设备性能偏好。 | MindSpore 执行偏好 | `openharmony/third_party/mindspore/interfaces/kits/c/context.h:318` |
| `OH_AI_DeviceInfoSetPriority` | MindSpore Lite 上下文/设备 | 设置 MindSpore 的设备调度优先级偏好。 | MindSpore 执行优先级 | `openharmony/third_party/mindspore/interfaces/kits/c/context.h:334` |
| `OH_AI_ModelBuild` | MindSpore Lite 模型 | 从内存中的模型数据按指定上下文构建 MindSpore 模型。 | 模型部署 | `openharmony/third_party/mindspore/interfaces/kits/c/model.h:96` |
| `OH_AI_ModelBuildFromFile` | MindSpore Lite 模型 | 从模型文件按指定上下文构建 MindSpore 模型。 | 模型部署 | `openharmony/third_party/mindspore/interfaces/kits/c/model.h:109` |
| `OH_AI_ModelCreate` | MindSpore Lite 模型 | 创建 MindSpore Lite 模型对象。 | 模型生命周期 | `openharmony/third_party/mindspore/interfaces/kits/c/model.h:75` |
| `OH_AI_ModelPredict` | MindSpore Lite 模型 | 使用已构建的 MindSpore 模型执行一次推理。 | 模型推理 | `openharmony/third_party/mindspore/interfaces/kits/c/model.h:136` |
| `OH_AI_ModelResize` | MindSpore Lite 模型 | 调整模型输入张量 shape，以支持受约束的动态输入。 | 动态 shape 配置 | `openharmony/third_party/mindspore/interfaces/kits/c/model.h:122` |
| `OH_AI_TensorSetUserData` | MindSpore Lite 张量 | 为 MindSpore 张量设置调用方数据缓冲区，可用于减少额外数据复制。 | 输入输出数据通路 | `openharmony/third_party/mindspore/interfaces/kits/c/tensor.h:206` |
| `OH_NNCompilation_Build` | NNRT 编译与设备选择 | 把配置好的编译对象构建为可执行模型。 | 编译完成门禁 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:353` |
| `OH_NNCompilation_Construct` | NNRT 编译与设备选择 | 以已完成的 NNRT 图创建编译对象，准备交给选定设备构建可执行模型。 | 在线图编译 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:74` |
| `OH_NNCompilation_ConstructWithOfflineModelFile` | NNRT 编译与设备选择 | 从设备厂商离线编译的模型文件创建编译对象。 | 离线模型部署 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:94` |
| `OH_NNCompilation_EnableFloat16` | NNRT 编译与设备选择 | 请求在支持的设备上启用 float16 计算路径。 | 精度/性能偏好 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:332` |
| `OH_NNCompilation_SetCache` | NNRT 编译与设备选择 | 配置已编译模型的缓存目录和版本，减少后续重编译。 | 编译缓存 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:265` |
| `OH_NNCompilation_SetDevice` | NNRT 编译与设备选择 | 选择当前运行时枚举到的设备进行模型编译和计算。 | 设备选择门禁 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:227` |
| `OH_NNCompilation_SetPerformanceMode` | NNRT 编译与设备选择 | 为编译/执行请求设置性能偏好，而非保证实际吞吐。 | 执行偏好 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:287` |
| `OH_NNCompilation_SetPriority` | NNRT 编译与设备选择 | 为模型推理设置调度优先级偏好。 | 执行优先级 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:310` |
| `OH_NNDevice_GetAllDevicesID` | NNRT 设备探测 | 枚举当前设备上 NNRT 可见的计算设备 ID。 | 运行时设备发现 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:1141` |
| `OH_NNDevice_GetName` | NNRT 设备探测 | 读取一个 NNRT 设备 ID 对应的运行时名称。 | 设备身份确认 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:1165` |
| `OH_NNDevice_GetType` | NNRT 设备探测 | 读取一个 NNRT 设备的 CPU、GPU 或专用加速器类型。 | 设备类型确认 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:1188` |
| `OH_NNExecutor_Construct` | NNRT 推理执行 | 从已构建的编译对象创建推理执行器。 | 执行器创建 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:842` |
| `OH_NNExecutor_RunAsync` | NNRT 推理执行 | 异步提交一次模型推理，并通过回调接收结果或服务异常。 | 异步推理 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:1112` |
| `OH_NNExecutor_RunSync` | NNRT 推理执行 | 同步执行一次模型推理并填充输出张量。 | 同步推理 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:1062` |
| `OH_NNExecutor_SetOnRunDone` | NNRT 推理执行 | 注册异步推理完成回调。 | 异步结果回调 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:1018` |
| `OH_NNExecutor_SetOnServiceDied` | NNRT 推理执行 | 注册设备驱动服务异常回调，以便重新编译或降级。 | 设备异常处理 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_core.h:1035` |
| `OH_NNModel_AddOperation` | NNRT 图模型 | 把一个 OH_NN_OPS 图算子及其输入、参数、输出连接到模型。 | 图算子装配 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_runtime.h:267` |
| `OH_NNModel_AddTensorToModel` | NNRT 图模型 | 把带 shape、dtype、format 的张量描述加入 NNRT 图。 | 张量建模 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_runtime.h:177` |
| `OH_NNModel_Construct` | NNRT 图模型 | 创建 NNRT 图模型对象，作为张量、算子和拓扑的容器。 | 图构建起点 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_runtime.h:151` |
| `OH_NNModel_Finish` | NNRT 图模型 | 冻结 NNRT 图组合；之后才能查询算子支持性或创建编译对象。 | 图完成门禁 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_runtime.h:323` |
| `OH_NNModel_GetAvailableOperations` | NNRT 图模型 | 查询指定运行时设备是否支持该完整模型中的每个算子。 | 按模型/设备的算子支持门禁 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_runtime.h:366` |
| `OH_NNModel_SetTensorData` | NNRT 图模型 | 为常量张量（例如权重）写入数据。 | 常量/权重装载 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_runtime.h:199` |
| `OH_NNModel_SetTensorQuantParams` | NNRT 图模型 | 为图中张量设置量化 scale、zero-point 等参数。 | 量化建模 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_runtime.h:216` |
| `OH_NNModel_SpecifyInputsAndOutputs` | NNRT 图模型 | 声明 NNRT 图的端到端输入和输出张量。 | 图边界声明 | `openharmony/interface/sdk_c/ai/neural_network_runtime/neural_network_runtime.h:296` |

## 108 个 NNRT 图算子

以下列表是候选检索目录。每一条的完整源码注释、shape/dtype/参数约束、静态匹配信号、严格案例关联和运行时门禁均在同目录的 `npu_api_knowledge_base.json` 中。

### 激活与非线性

逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 应优先作为图融合节点，而不是为一个独立小函数新建 NPU 调用。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_HSWISH` | Hard-Swish 激活：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |
| `OH_NN_OPS_SIGMOID` | Sigmoid 激活：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |
| `OH_NN_OPS_SOFTMAX` | Softmax 激活：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |
| `OH_NN_OPS_TANH` | 双曲正切激活：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |
| `OH_NN_OPS_PRELU` | 参数化 ReLU：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |
| `OH_NN_OPS_RELU` | ReLU 激活：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |
| `OH_NN_OPS_RELU6` | ReLU6 激活：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |
| `OH_NN_OPS_GELU` | GELU 激活：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |
| `OH_NN_OPS_LEAKY_RELU` | Leaky-ReLU 激活：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |
| `OH_NN_OPS_LOG_SOFTMAX` | Log-Softmax：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |
| `OH_NN_OPS_SWISH` | Swish 激活：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |
| `OH_NN_OPS_HARD_SIGMOID` | Hard-Sigmoid 激活：逐元素应用常见神经网络非线性函数，通常适合并入相邻卷积或矩阵节点。 | 数组循环中的阈值/指数/双曲函数；clip/min/max 分段 | 无 |

### 卷积与空间滤波

对规则网格上的局部邻域做加权累加，通常对应图像或特征图的空间卷积。 通常应与相邻 bias/activation/resize 等节点组成连续图；单次小尺寸调用可能被编译和搬运成本淹没。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_CONV2D` | 二维卷积：对规则网格上的局部邻域做加权累加，通常对应图像或特征图的空间卷积。 | 二维/三维嵌套空间循环；固定或循环不变量的卷积核/权重 | mindspore_kirin_npu_conv2d_fusion_to_cpu_kernel_fallback |
| `OH_NN_OPS_CONV2D_TRANSPOSE` | 二维转置卷积：对规则网格上的局部邻域做加权累加，通常对应图像或特征图的空间卷积。 | 二维/三维嵌套空间循环；固定或循环不变量的卷积核/权重 | mindspore_kirin_npu_conv2d_transpose_fusion_to_cpu_kernel_fallback |
| `OH_NN_OPS_DEPTHWISE_CONV2D_NATIVE` | 二维深度可分离卷积：对规则网格上的局部邻域做加权累加，通常对应图像或特征图的空间卷积。 | 二维/三维嵌套空间循环；固定或循环不变量的卷积核/权重 | 无 |

### 逐元素算术

对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 单一逐元素算子往往太轻；只有能与相邻图节点合并、数据已在推理图中或批量足够大时才值得继续评估。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_ADD` | 逐元素加法：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | mindspore_kirin_npu_add_fusion_to_cpu_kernel_fallback |
| `OH_NN_OPS_BIAS_ADD` | 偏置加法：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | 无 |
| `OH_NN_OPS_DIV` | 逐元素除法：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | mindspore_kirin_npu_div_fusion_to_cpu_kernel_fallback |
| `OH_NN_OPS_ELTWISE` | 逐元素组合：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | mindspore_kirin_npu_eltwise_to_cpu_kernel_fallback |
| `OH_NN_OPS_MAXIMUM` | 逐元素最大值：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | mindspore_kirin_npu_maximum_to_cpu_kernel_fallback |
| `OH_NN_OPS_MUL` | 逐元素乘法：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | mindspore_kirin_npu_mul_fusion_to_cpu_kernel_fallback |
| `OH_NN_OPS_POW` | 逐元素幂运算：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | 无 |
| `OH_NN_OPS_SCALE` | 缩放：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | 无 |
| `OH_NN_OPS_SQUARED_DIFFERENCE` | 平方差：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | 无 |
| `OH_NN_OPS_SUB` | 逐元素减法：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | mindspore_kirin_npu_sub_fusion_to_cpu_kernel_fallback |
| `OH_NN_OPS_ABS` | 绝对值：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | 无 |
| `OH_NN_OPS_SQUARE` | 平方：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | 无 |
| `OH_NN_OPS_CLIP` | 区间裁剪：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | 无 |
| `OH_NN_OPS_MOD` | 取模：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | 无 |
| `OH_NN_OPS_NEG` | 取负：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | 无 |
| `OH_NN_OPS_MINIMUM` | 逐元素最小值：对同形状或可广播张量逐元素进行算术处理，迭代之间通常没有数据依赖。 | 大规模数组/张量遍历；相同下标读取多个输入 | mindspore_kirin_npu_minimum_to_cpu_kernel_fallback |

### 索引、切片与数据构造

根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 单独的索引和数据构造往往受内存访问限制；适合在完整图中避免中间张量落回 CPU。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_CAST` | 数据类型转换：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | mindspore_kirin_npu_cast_to_cpu_kernel_fallback |
| `OH_NN_OPS_FILL` | 常量填充：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_GATHER` | 按索引聚集：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_ONE_HOT` | 独热编码：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_PAD` | 边界填充：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_SHAPE` | 读取张量形状：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_SLICE` | 切片：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_STRIDED_SLICE` | 带步长切片：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_QUANT_DTYPE_CAST` | 量化数据类型转换：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_RANGE` | 数值范围生成：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_CONSTANT_OF_SHAPE` | 按形状生成常量：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_SPARSE_TO_DENSE` | 稀疏转稠密：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_SCATTER_ND` | 多维散射写入：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |
| `OH_NN_OPS_GATHER_ND` | 多维索引聚集：根据索引、范围或 padding 规则读写张量，包含 gather/scatter/slice/fill 等操作。 | 索引数组；边界判断 | 无 |

### 比较与逻辑

逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 通常需要和产生/消费掩码的计算节点一起图化，不能仅因出现条件分支而推荐 NPU。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_LESS_EQUAL` | 小于等于比较：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | 无 |
| `OH_NN_OPS_LESS` | 小于比较：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | 无 |
| `OH_NN_OPS_SELECT` | 条件选择：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | 无 |
| `OH_NN_OPS_EQUAL` | 等于比较：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | 无 |
| `OH_NN_OPS_GREATER` | 大于比较：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | 无 |
| `OH_NN_OPS_NOT_EQUAL` | 不等于比较：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | 无 |
| `OH_NN_OPS_GREATER_EQUAL` | 大于等于比较：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | 无 |
| `OH_NN_OPS_ASSERT` | 断言：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | 无 |
| `OH_NN_OPS_LOGICAL_AND` | 逻辑与：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | mindspore_kirin_npu_logical_and_to_cpu_kernel_fallback |
| `OH_NN_OPS_LOGICAL_NOT` | 逻辑非：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | 无 |
| `OH_NN_OPS_WHERE` | 条件选择：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | 无 |
| `OH_NN_OPS_LOGICAL_OR` | 逻辑或：逐元素比较或组合布尔条件，常为选择、掩码或后处理图的辅助节点。 | 数组循环中的比较；布尔掩码 | mindspore_kirin_npu_logical_or_to_cpu_kernel_fallback |

### 矩阵乘与全连接

对张量维度做乘加归约，是神经网络推理中最典型的高算术强度计算。 需要把可观察到的循环核心、维度和权重生命周期变成完整图；仅看到一次标量乘法不构成候选。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_FULL_CONNECTION` | 全连接层：对张量维度做乘加归约，是神经网络推理中最典型的高算术强度计算。 | 至少两层独立循环；乘加累积 | mindspore_kirin_npu_full_connection_to_cpu_kernel_fallback |
| `OH_NN_OPS_MATMUL` | 矩阵乘：对张量维度做乘加归约，是神经网络推理中最典型的高算术强度计算。 | 至少两层独立循环；乘加累积 | mindspore_kirin_npu_mat_mul_fusion_to_cpu_kernel_fallback |

### 归一化

沿通道或特征维度计算统计量并缩放/平移输入张量。 归一化的收益依赖其与卷积/激活的连续图关系；需要验证布局变换和临时张量成本。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_BATCH_NORM` | 批归一化：沿通道或特征维度计算统计量并缩放/平移输入张量。 | 均值/方差或平方和归约；通道循环 | 无 |
| `OH_NN_OPS_LAYER_NORM` | 层归一化：沿通道或特征维度计算统计量并缩放/平移输入张量。 | 均值/方差或平方和归约；通道循环 | 无 |
| `OH_NN_OPS_INSTANCE_NORM` | 实例归一化：沿通道或特征维度计算统计量并缩放/平移输入张量。 | 均值/方差或平方和归约；通道循环 | mindspore_kirin_npu_instance_norm_to_cpu_kernel_fallback |
| `OH_NN_OPS_L2_NORMALIZE` | L2 归一化：沿通道或特征维度计算统计量并缩放/平移输入张量。 | 均值/方差或平方和归约；通道循环 | 无 |
| `OH_NN_OPS_LRN` | 局部响应归一化：沿通道或特征维度计算统计量并缩放/平移输入张量。 | 均值/方差或平方和归约；通道循环 | 无 |

### 池化与窗口归约

在空间窗口内做平均或极值归约，常位于视觉/神经网络特征图流水线。 单独池化通常不足以抵消图构建成本，适合与前后神经网络节点融合。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_AVG_POOL` | 二维平均池化：在空间窗口内做平均或极值归约，常位于视觉/神经网络特征图流水线。 | 规则滑动窗口；局部 max/sum/count 累加 | 无 |
| `OH_NN_OPS_MAX_POOL` | 二维最大池化：在空间窗口内做平均或极值归约，常位于视觉/神经网络特征图流水线。 | 规则滑动窗口；局部 max/sum/count 累加 | 无 |

### 归约与排序选择

沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 必须检查归约规模和前后节点；小归约或频繁 CPU/NPU 往返不一定受益。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_REDUCE_MEAN` | 均值归约：沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 | 循环累加/比较；axis 或维度参数 | 无 |
| `OH_NN_OPS_REDUCE_PROD` | 乘积归约：沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 | 循环累加/比较；axis 或维度参数 | 无 |
| `OH_NN_OPS_REDUCE_ALL` | 布尔全量归约：沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 | 循环累加/比较；axis 或维度参数 | 无 |
| `OH_NN_OPS_TOP_K` | Top-K 选择：沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 | 循环累加/比较；axis 或维度参数 | 无 |
| `OH_NN_OPS_ARG_MAX` | 最大值索引：沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 | 循环累加/比较；axis 或维度参数 | 无 |
| `OH_NN_OPS_ALL` | 布尔全量归约：沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 | 循环累加/比较；axis 或维度参数 | 无 |
| `OH_NN_OPS_RANK` | 张量秩：沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 | 循环累加/比较；axis 或维度参数 | 无 |
| `OH_NN_OPS_REDUCE_MAX` | 最大值归约：沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 | 循环累加/比较；axis 或维度参数 | 无 |
| `OH_NN_OPS_REDUCE_MIN` | 最小值归约：沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 | 循环累加/比较；axis 或维度参数 | 无 |
| `OH_NN_OPS_REDUCE_SUM` | 求和归约：沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 | 循环累加/比较；axis 或维度参数 | 无 |
| `OH_NN_OPS_REDUCE_L2` | L2 范数归约：沿指定轴把多个元素聚合为统计量、极值、索引或 Top-K 结果。 | 循环累加/比较；axis 或维度参数 | 无 |

### 循环神经网络

维护时序状态并在序列维度迭代计算门控/矩阵变换。 必须能明确恢复 LSTM 图语义和状态契约；普通业务状态机不应误匹配到此类。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_LSTM` | 长短期记忆网络：维护时序状态并在序列维度迭代计算门控/矩阵变换。 | 时间步循环；隐藏状态/细胞状态 | 无 |

### 形状与布局变换

重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 通常只应作为连续 NNRT 图的一部分；若它是独立内存搬运热点，应先比较 CPU SIMD、GPU blit 和 NPU 图转换成本。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_BATCH_TO_SPACE_ND` | 批次到空间重排：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_CONCAT` | 张量拼接：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_EXPAND_DIMS` | 扩展维度：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_SPACE_TO_BATCH_ND` | 空间到批次重排：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_SPLIT` | 张量切分：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | mindspore_kirin_npu_split_to_cpu_kernel_fallback |
| `OH_NN_OPS_SQUEEZE` | 压缩维度：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_STACK` | 堆叠张量：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_TILE` | 张量平铺复制：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_TRANSPOSE` | 维度转置：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_RESHAPE` | 重塑形状：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_UNSQUEEZE` | 插入长度为一的维度：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_UNSTACK` | 沿轴拆分张量：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | mindspore_kirin_npu_unstack_to_cpu_kernel_fallback |
| `OH_NN_OPS_FLATTEN` | 展平：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_DEPTH_TO_SPACE` | 深度到空间重排：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |
| `OH_NN_OPS_BROADCAST_TO` | 广播到目标形状：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | mindspore_kirin_npu_broadcast_to_to_cpu_kernel_fallback |
| `OH_NN_OPS_SPACE_TO_DEPTH` | 空间到深度重排：重排、切分、拼接或广播张量形状/布局，本身常是图连接节点而非主计算热点。 | 索引重映射；shape/dimension 元数据 | 无 |

### 一元数学函数

对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 需要在连续图中评估；一个小型数学循环更可能适合 CPU 向量化而不是单独 NPU 调用。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_SQRT` | 平方根：对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 | 大规模数组循环；sqrt/exp/log/sin/cos 等数学调用 | 无 |
| `OH_NN_OPS_RSQRT` | 平方根倒数：对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 | 大规模数组循环；sqrt/exp/log/sin/cos 等数学调用 | 无 |
| `OH_NN_OPS_ERF` | 误差函数：对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 | 大规模数组循环；sqrt/exp/log/sin/cos 等数学调用 | 无 |
| `OH_NN_OPS_EXP` | 自然指数：对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 | 大规模数组循环；sqrt/exp/log/sin/cos 等数学调用 | 无 |
| `OH_NN_OPS_COS` | 余弦：对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 | 大规模数组循环；sqrt/exp/log/sin/cos 等数学调用 | 无 |
| `OH_NN_OPS_LOG` | 自然对数：对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 | 大规模数组循环；sqrt/exp/log/sin/cos 等数学调用 | 无 |
| `OH_NN_OPS_RECIPROCAL` | 倒数：对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 | 大规模数组循环；sqrt/exp/log/sin/cos 等数学调用 | 无 |
| `OH_NN_OPS_SIN` | 正弦：对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 | 大规模数组循环；sqrt/exp/log/sin/cos 等数学调用 | 无 |
| `OH_NN_OPS_CEIL` | 向上取整：对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 | 大规模数组循环；sqrt/exp/log/sin/cos 等数学调用 | 无 |
| `OH_NN_OPS_FLOOR` | 向下取整：对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 | 大规模数组循环；sqrt/exp/log/sin/cos 等数学调用 | 无 |
| `OH_NN_OPS_ROUND` | 四舍五入：对张量逐元素计算绝对值、指数、对数、三角或倒数等数学函数。 | 大规模数组循环；sqrt/exp/log/sin/cos 等数学调用 | 无 |

### 视觉后处理

对检测框、评分或图像区域做模型后处理，语义与输入张量约束较强。 先确认该逻辑是不是模型图的一部分；任意图像业务代码不能仅凭关键词视为检测模型后处理。

| 算子 | 一句话摘要 | AST/代码信号 | 严格案例 |
|---|---|---|---|
| `OH_NN_OPS_RESIZE_BILINEAR` | 双线性缩放：对检测框、评分或图像区域做模型后处理，语义与输入张量约束较强。 | 候选框/score 张量；坐标变换 | 无 |
| `OH_NN_OPS_CROP` | 图像/特征裁剪：对检测框、评分或图像区域做模型后处理，语义与输入张量约束较强。 | 候选框/score 张量；坐标变换 | 无 |
| `OH_NN_OPS_DETECTION_POST_PROCESS` | 检测结果后处理：对检测框、评分或图像区域做模型后处理，语义与输入张量约束较强。 | 候选框/score 张量；坐标变换 | 无 |

## 详细记录如何使用

JSON 中每条 `npu_operation` 含有：`one_line_abstract_zh`、`detailed_description_zh`、完整的源码文档摘录、`source_constraints_en`、AST/数据形态匹配信号、构图入口、严格案例/内部 delegate 关联、以及固定的运行时验证步骤。每条 `npu_api_entrypoint` 则描述从建图、设备选择、编译到同步/异步执行的调用职责。

HiAI 部分只保留为 MindSpore 内部 delegate 的源码使用证据。由于当前工作区没有发现相应的应用侧公开 ABI 合同，平台不会把 `hiai::op::*` 作为可直接建议给业务代码的 API。

## 构建与校验

```bash
python3 docs_ffrt/tools/build_npu_api_knowledge_base.py
python3 docs_ffrt/tools/build_npu_api_knowledge_base.py --check
```

产物 SHA-256：`213e6771c9ced8d0104fa8f59021e8f9cf03fd2b23eef8cbd0ecd4725d90f0d5`
