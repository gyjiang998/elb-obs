# OpenHarmony GPU API 知识库

> 自动生成：`python3 docs_ffrt/tools/build_accelerator_api_knowledge_bases.py`。不要手工编辑生成文件。

## 定位与边界

候选相关的 Vulkan/OpenGL ES SDK 入口，以及当前源码已有的 GPU 图像处理集成路径。它不是完整 Vulkan/OpenGL API 镜像。

所有记录的目标设备可用性均为 `evidence_required`。接口/源码路径存在不等于设备驱动、格式、权限、实现或收益已经可用。

## 内容统计

- API/集成入口：18
- 同一硬件单元的严格 fallback 案例：7
- 与具体入口直接关联 strict case 的记录：5

## 运行时验证协议

1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

## API 目录

| API / 集成入口 | 语义族 | 暴露层级 | 一句话摘要 | 直接 strict case |
|---|---|---|---|---|
| `AstcCodec::TryAstcEncBasedOnCl` | GPU 图像效果路径 | system_source_integration | 调用当前源码中的 ASTC GPU/OpenCL 编码 wrapper。 | astc_opencl_encode_to_cpu_software_fallback |
| `GpuBrightnessAlgo::OnApplyRGBA8888` | GPU 图像效果路径 | system_source_integration | 使用当前 image effect 的 GPU 算法处理 RGBA8888 亮度。 | efilter_brightness_gpu_cpu_mapping |
| `GpuContrastAlgo::OnApplyRGBA8888` | GPU 图像效果路径 | system_source_integration | 使用当前 image effect 的 GPU 算法处理 RGBA8888 对比度。 | efilter_contrast_gpu_cpu_mapping |
| `ImageCompressor::GpuCompress` | GPU 图像效果路径 | system_source_integration | 使用 ArkUI 源码中的 GPU 图像压缩路径。 | image_compress_gpu_to_raster_fallback |
| `PixelMapPostProcWithGL` | GPU 图像效果路径 | system_source_integration | 对 PixelMap 执行 OpenGL 缩放/旋转等后处理并支持 CPU 回退。 | pixelmap_gpu_rotate_to_cpu_fallback, pixelmap_gpu_scale_to_cpu_fallback |
| `glDispatchCompute` | GPU 计算管线 | third_party_sdk_header | 在支持 ES 3.1 的 context 中派发 compute shader。 | 无 |
| `glDrawArrays` | GPU 计算管线 | third_party_sdk_header | 以数组顶点提交 draw call，常用于全屏 fragment shader 图像处理。 | 无 |
| `glFramebufferTexture2D` | GPU 图像/Buffer 传输 | third_party_sdk_header | 把纹理附着到 framebuffer 作为 GPU 渲染输出。 | 无 |
| `glTexImage2D` | GPU 图像/Buffer 传输 | third_party_sdk_header | 把二维像素数据定义或上传为 OpenGL ES texture。 | 无 |
| `glUseProgram` | GPU 计算管线 | third_party_sdk_header | 绑定用于后续 draw/compute 的 shader program。 | 无 |
| `vkCmdBlitImage` | GPU 图像/Buffer 传输 | third_party_sdk_header | 在两个 GPU image 间执行缩放或过滤式 blit。 | 无 |
| `vkCmdCopyBufferToImage` | GPU 图像/Buffer 传输 | third_party_sdk_header | 把线性 buffer 数据复制到 GPU image。 | 无 |
| `vkCmdCopyImageToBuffer` | GPU 图像/Buffer 传输 | third_party_sdk_header | 把 GPU image 复制回线性 buffer。 | 无 |
| `vkCmdDispatch` | GPU 计算管线 | third_party_sdk_header | 按三维工作组把 compute shader 派发到 GPU。 | 无 |
| `vkCreateComputePipelines` | GPU 计算管线 | third_party_sdk_header | 创建可提交到 GPU 的 Vulkan compute pipeline。 | 无 |
| `vkGetMemoryNativeBufferOHOS` | GPU 图像/Buffer 传输 | openharmony_sdk_header | 从 Vulkan device memory 获取 OH_NativeBuffer。 | 无 |
| `vkGetNativeBufferPropertiesOHOS` | GPU 图像/Buffer 传输 | openharmony_sdk_header | 查询 OH_NativeBuffer 与 Vulkan device 互操作所需的属性。 | 无 |
| `vkQueueSubmit` | GPU 计算管线 | third_party_sdk_header | 把命令缓冲及同步对象提交给 GPU queue。 | 无 |

## 详细记录

### `AstcCodec::TryAstcEncBasedOnCl`

调用当前源码中的 ASTC GPU/OpenCL 编码 wrapper。

该系统集成路径创建 ASTC CL encoder、填充图像描述、执行编码并关闭 handle。它与软件编码存在严格回退 pair，但底层 AstcCl ABI 的应用侧公开合同未由当前工作区建立。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/texture_encode/astc_codec.cpp:500`；库：image framework internal integration；SystemCapability：not asserted for application SDK。
- **代码/AST 信号**：RGBA8888 或像素 buffer；逐像素颜色/几何变换；图像尺寸与 stride；图像效果或压缩语义。
- **数据形态**：RGBA 像素块；Image/PixelMap/Bitmap；输入输出 buffer 和显式格式。
- **Top-N 匹配别名**：astc, opencl_encode, texture_encode, tryastcencbasedoncl。
- **单独卸载边界**：这些是系统源码集成路径，不等于第三方应用可直接链接的公共 SDK；需先确认所属模块和可用图形上下文。
- **直接 strict case**：astc_opencl_encode_to_cpu_software_fallback。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `GpuBrightnessAlgo::OnApplyRGBA8888`

使用当前 image effect 的 GPU 算法处理 RGBA8888 亮度。

这是用户看到的已有 GPU 实现路径。它对应明确的 RGBA 输入/输出 effect buffer；推荐复用时必须继续验证该模块、图形 context 和输入 format，而非自行假定可调用。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/image_effect/frameworks/native/efilter/filterimpl/brightness/gpu_brightness_algo.cpp:112`；库：image effect internal integration；SystemCapability：not asserted for application SDK。
- **代码/AST 信号**：RGBA8888 或像素 buffer；逐像素颜色/几何变换；图像尺寸与 stride；图像效果或压缩语义。
- **数据形态**：RGBA 像素块；Image/PixelMap/Bitmap；输入输出 buffer 和显式格式。
- **Top-N 匹配别名**：brightness, gpubrightnessalgo, image_effect, rgba8888。
- **单独卸载边界**：这些是系统源码集成路径，不等于第三方应用可直接链接的公共 SDK；需先确认所属模块和可用图形上下文。
- **直接 strict case**：efilter_brightness_gpu_cpu_mapping。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `GpuContrastAlgo::OnApplyRGBA8888`

使用当前 image effect 的 GPU 算法处理 RGBA8888 对比度。

该路径是当前源码中的对比度 GPU/CPU 对照实现。它给出可复用的效果级语义，但不证明任意颜色循环都能接入该内部算法。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/image_effect/frameworks/native/efilter/filterimpl/contrast/gpu_contrast_algo.cpp:113`；库：image effect internal integration；SystemCapability：not asserted for application SDK。
- **代码/AST 信号**：RGBA8888 或像素 buffer；逐像素颜色/几何变换；图像尺寸与 stride；图像效果或压缩语义。
- **数据形态**：RGBA 像素块；Image/PixelMap/Bitmap；输入输出 buffer 和显式格式。
- **Top-N 匹配别名**：contrast, gpucontrastalgo, image_effect, rgba8888。
- **单独卸载边界**：这些是系统源码集成路径，不等于第三方应用可直接链接的公共 SDK；需先确认所属模块和可用图形上下文。
- **直接 strict case**：efilter_contrast_gpu_cpu_mapping。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `ImageCompressor::GpuCompress`

使用 ArkUI 源码中的 GPU 图像压缩路径。

该入口面向 ArkUI/RSBitmap 数据路径；严格案例证明它可回退到 raster，但不证明它是任意应用可调用的通用压缩 API。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/arkui/ace_engine/frameworks/core/image/image_compressor.cpp:161`；库：ArkUI internal integration；SystemCapability：not asserted for application SDK。
- **代码/AST 信号**：RGBA8888 或像素 buffer；逐像素颜色/几何变换；图像尺寸与 stride；图像效果或压缩语义。
- **数据形态**：RGBA 像素块；Image/PixelMap/Bitmap；输入输出 buffer 和显式格式。
- **Top-N 匹配别名**：arkui_image, gpucompress, image_compress, rsbitmap。
- **单独卸载边界**：这些是系统源码集成路径，不等于第三方应用可直接链接的公共 SDK；需先确认所属模块和可用图形上下文。
- **直接 strict case**：image_compress_gpu_to_raster_fallback。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `PixelMapPostProcWithGL`

对 PixelMap 执行 OpenGL 缩放/旋转等后处理并支持 CPU 回退。

源码路径检查 PixelMap 形态、DMA 写回限制、RGBA format、GL context 和 shader program；这正是图像 GPU 候选需要先评估数据通路的证据。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/image_framework/frameworks/innerkitsimpl/converter/src/post_proc.cpp:915`；库：image framework internal integration；SystemCapability：not asserted for application SDK。
- **代码/AST 信号**：RGBA8888 或像素 buffer；逐像素颜色/几何变换；图像尺寸与 stride；图像效果或压缩语义。
- **数据形态**：RGBA 像素块；Image/PixelMap/Bitmap；输入输出 buffer 和显式格式。
- **Top-N 匹配别名**：gl_resize, pixelmap, pixelmappostprocwithgl, rotate, scale。
- **单独卸载边界**：这些是系统源码集成路径，不等于第三方应用可直接链接的公共 SDK；需先确认所属模块和可用图形上下文。
- **直接 strict case**：pixelmap_gpu_rotate_to_cpu_fallback, pixelmap_gpu_scale_to_cpu_fallback。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `glDispatchCompute`

在支持 ES 3.1 的 context 中派发 compute shader。

这是 OpenGL ES 计算路径的显式执行入口；候选代码仍需提供工作组映射、SSBO/image binding、barrier 和 GPU-CPU 同步设计。

- **暴露层级**：`third_party_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/openGLES/GLES3/gl31.h:1432`；库：OpenGL ES loader（目标镜像验证）；SystemCapability：not asserted by third-party header。
- **代码/AST 信号**：二维或三维独立循环；规则数组/图像下标访问；每像素或每元素独立写回；可提取为无跨迭代依赖的 kernel。
- **数据形态**：连续 buffer 或图像纹理；明确宽高/stride/format；可描述的输入输出边界。
- **Top-N 匹配别名**：compute_shader, gldispatchcompute, ssbo, workgroup。
- **单独卸载边界**：必须先建立 shader、pipeline 和同步边界；小任务或频繁 CPU-GPU 往返通常不值得独立卸载。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `glDrawArrays`

以数组顶点提交 draw call，常用于全屏 fragment shader 图像处理。

对全屏 quad 和 fragment shader 形式的图像效果，该调用是实际 GPU 执行点之一；仍需验证顶点/纹理/输出 framebuffer 全链路。

- **暴露层级**：`third_party_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/openGLES/GLES3/gl31.h:547`；库：OpenGL ES loader（目标镜像验证）；SystemCapability：not asserted by third-party header。
- **代码/AST 信号**：二维或三维独立循环；规则数组/图像下标访问；每像素或每元素独立写回；可提取为无跨迭代依赖的 kernel。
- **数据形态**：连续 buffer 或图像纹理；明确宽高/stride/format；可描述的输入输出边界。
- **Top-N 匹配别名**：draw_call, fragment_shader, fullscreen_quad, gldrawarrays。
- **单独卸载边界**：必须先建立 shader、pipeline 和同步边界；小任务或频繁 CPU-GPU 往返通常不值得独立卸载。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `glFramebufferTexture2D`

把纹理附着到 framebuffer 作为 GPU 渲染输出。

图像处理通常需要把 GPU 结果写入 offscreen framebuffer；需要检查 framebuffer 完整性、纹理格式和后续 readback/NativeBuffer 路径。

- **暴露层级**：`third_party_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/openGLES/GLES3/gl31.h:554`；库：OpenGL ES loader（目标镜像验证）；SystemCapability：not asserted by third-party header。
- **代码/AST 信号**：RGBA/像素/bitmap 数据；width/height/stride/format；缩放、旋转、裁剪或颜色变换；buffer/texture 生命周期。
- **数据形态**：PixelMap/Bitmap/NativeBuffer；纹理格式与行跨度；DMA 或共享 buffer 约束。
- **Top-N 匹配别名**：framebuffer, glframebuffertexture2d, offscreen, render_target。
- **单独卸载边界**：传输 API 自身不是计算收益证明；必须把 upload/download、布局转换和 fence/sync 纳入基准。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `glTexImage2D`

把二维像素数据定义或上传为 OpenGL ES texture。

适合 RGBA 图像效果、缩放、旋转和 fragment shader 处理的输入通路；format/type/row layout 与 context 可用性是硬条件。

- **暴露层级**：`third_party_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/openGLES/GLES3/gl31.h:612`；库：OpenGL ES loader（目标镜像验证）；SystemCapability：not asserted by third-party header。
- **代码/AST 信号**：RGBA/像素/bitmap 数据；width/height/stride/format；缩放、旋转、裁剪或颜色变换；buffer/texture 生命周期。
- **数据形态**：PixelMap/Bitmap/NativeBuffer；纹理格式与行跨度；DMA 或共享 buffer 约束。
- **Top-N 匹配别名**：glteximage2d, pixel_upload, rgba_texture, texture_upload。
- **单独卸载边界**：传输 API 自身不是计算收益证明；必须把 upload/download、布局转换和 fence/sync 纳入基准。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `glUseProgram`

绑定用于后续 draw/compute 的 shader program。

GPU 像素效果必须有已链接的 shader program；仅存在 brightness/contrast 算术不说明 shader 编译、uniform 和格式路径都可建立。

- **暴露层级**：`third_party_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/openGLES/GLES3/gl31.h:637`；库：OpenGL ES loader（目标镜像验证）；SystemCapability：not asserted by third-party header。
- **代码/AST 信号**：二维或三维独立循环；规则数组/图像下标访问；每像素或每元素独立写回；可提取为无跨迭代依赖的 kernel。
- **数据形态**：连续 buffer 或图像纹理；明确宽高/stride/format；可描述的输入输出边界。
- **Top-N 匹配别名**：fragment_shader, gluseprogram, gpu_shader, shader_program。
- **单独卸载边界**：必须先建立 shader、pipeline 和同步边界；小任务或频繁 CPU-GPU 往返通常不值得独立卸载。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `vkCmdBlitImage`

在两个 GPU image 间执行缩放或过滤式 blit。

适合现有 image/image 路径中的缩放、旋转前后布局或采样处理；它不是通用图像算法替代，需要确认 filter、format 和 image usage。

- **暴露层级**：`third_party_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/vulkan-headers/vulkan/vulkan_core.h:4281`；库：Vulkan loader（目标镜像验证）；SystemCapability：not asserted by third-party header。
- **代码/AST 信号**：RGBA/像素/bitmap 数据；width/height/stride/format；缩放、旋转、裁剪或颜色变换；buffer/texture 生命周期。
- **数据形态**：PixelMap/Bitmap/NativeBuffer；纹理格式与行跨度；DMA 或共享 buffer 约束。
- **Top-N 匹配别名**：blit, resize, scale_image, vkcmdblitimage。
- **单独卸载边界**：传输 API 自身不是计算收益证明；必须把 upload/download、布局转换和 fence/sync 纳入基准。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `vkCmdCopyBufferToImage`

把线性 buffer 数据复制到 GPU image。

这是将 CPU 像素/张量数据放入 GPU image 的典型路径；必须验证 format、layout、row pitch 和 copy region，并把上传成本计入收益。

- **暴露层级**：`third_party_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/vulkan-headers/vulkan/vulkan_core.h:4282`；库：Vulkan loader（目标镜像验证）；SystemCapability：not asserted by third-party header。
- **代码/AST 信号**：RGBA/像素/bitmap 数据；width/height/stride/format；缩放、旋转、裁剪或颜色变换；buffer/texture 生命周期。
- **数据形态**：PixelMap/Bitmap/NativeBuffer；纹理格式与行跨度；DMA 或共享 buffer 约束。
- **Top-N 匹配别名**：buffer_to_image, texture_upload, upload_texture, vkcmdbuffertoimage。
- **单独卸载边界**：传输 API 自身不是计算收益证明；必须把 upload/download、布局转换和 fence/sync 纳入基准。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `vkCmdCopyImageToBuffer`

把 GPU image 复制回线性 buffer。

该路径把 GPU 输出带回 CPU；对只有一次小型图像操作的候选，回读与同步成本常常决定是否不应卸载。

- **暴露层级**：`third_party_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/vulkan-headers/vulkan/vulkan_core.h:4283`；库：Vulkan loader（目标镜像验证）；SystemCapability：not asserted by third-party header。
- **代码/AST 信号**：RGBA/像素/bitmap 数据；width/height/stride/format；缩放、旋转、裁剪或颜色变换；buffer/texture 生命周期。
- **数据形态**：PixelMap/Bitmap/NativeBuffer；纹理格式与行跨度；DMA 或共享 buffer 约束。
- **Top-N 匹配别名**：download_texture, image_to_buffer, readback, vkcmdimagetobuffer。
- **单独卸载边界**：传输 API 自身不是计算收益证明；必须把 upload/download、布局转换和 fence/sync 纳入基准。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `vkCmdDispatch`

按三维工作组把 compute shader 派发到 GPU。

该调用只提交已经绑定并构建好的 compute pipeline；候选代码需要可证明的独立数据并行、工作组边界和读写资源，否则无法从 CPU 循环自动转换。

- **暴露层级**：`third_party_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/vulkan-headers/vulkan/vulkan_core.h:4277`；库：Vulkan loader（目标镜像验证）；SystemCapability：not asserted by third-party header。
- **代码/AST 信号**：二维或三维独立循环；规则数组/图像下标访问；每像素或每元素独立写回；可提取为无跨迭代依赖的 kernel。
- **数据形态**：连续 buffer 或图像纹理；明确宽高/stride/format；可描述的输入输出边界。
- **Top-N 匹配别名**：compute_shader, dispatch, vkcmddispatch, workgroup。
- **单独卸载边界**：必须先建立 shader、pipeline 和同步边界；小任务或频繁 CPU-GPU 往返通常不值得独立卸载。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `vkCreateComputePipelines`

创建可提交到 GPU 的 Vulkan compute pipeline。

把已编译 shader 和 descriptor/layout 配置组成计算管线；热点代码必须能收敛成明确的 buffer/image 输入输出和工作组映射，不能仅凭存在循环推断 Vulkan 可用。

- **暴露层级**：`third_party_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/vulkan-headers/vulkan/vulkan_core.h:4233`；库：Vulkan loader（目标镜像验证）；SystemCapability：not asserted by third-party header。
- **代码/AST 信号**：二维或三维独立循环；规则数组/图像下标访问；每像素或每元素独立写回；可提取为无跨迭代依赖的 kernel。
- **数据形态**：连续 buffer 或图像纹理；明确宽高/stride/format；可描述的输入输出边界。
- **Top-N 匹配别名**：compute_pipeline, shader, spirv, vkcreatecomputepipelines。
- **单独卸载边界**：必须先建立 shader、pipeline 和同步边界；小任务或频繁 CPU-GPU 往返通常不值得独立卸载。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `vkGetMemoryNativeBufferOHOS`

从 Vulkan device memory 获取 OH_NativeBuffer。

该入口服务于 Vulkan memory 到 OpenHarmony NativeBuffer 的共享/交接；必须验证 memory allocation、usage、ownership 和消费端格式，不能假定零拷贝。

- **暴露层级**：`openharmony_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/vulkan-headers/vulkan/vulkan_ohos.h:534`；库：Vulkan loader（目标镜像验证）；SystemCapability：OpenHarmony Vulkan platform extension。
- **代码/AST 信号**：RGBA/像素/bitmap 数据；width/height/stride/format；缩放、旋转、裁剪或颜色变换；buffer/texture 生命周期。
- **数据形态**：PixelMap/Bitmap/NativeBuffer；纹理格式与行跨度；DMA 或共享 buffer 约束。
- **Top-N 匹配别名**：memory_nativebuffer, nativebuffer_export, vkgetmemorynativebufferohos。
- **单独卸载边界**：传输 API 自身不是计算收益证明；必须把 upload/download、布局转换和 fence/sync 纳入基准。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `vkGetNativeBufferPropertiesOHOS`

查询 OH_NativeBuffer 与 Vulkan device 互操作所需的属性。

这是 OpenHarmony 特有的 NativeBuffer 互操作入口。它帮助判断外部 buffer 的 memory type/format 路径，但不证明该 buffer 可用于目标 pipeline 或可零拷贝。

- **暴露层级**：`openharmony_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/vulkan-headers/vulkan/vulkan_ohos.h:517`；库：Vulkan loader（目标镜像验证）；SystemCapability：OpenHarmony Vulkan platform extension。
- **代码/AST 信号**：RGBA/像素/bitmap 数据；width/height/stride/format；缩放、旋转、裁剪或颜色变换；buffer/texture 生命周期。
- **数据形态**：PixelMap/Bitmap/NativeBuffer；纹理格式与行跨度；DMA 或共享 buffer 约束。
- **Top-N 匹配别名**：dma_buffer, nativebuffer, oh_nativebuffer, vkgetnativebufferpropertiesohos。
- **单独卸载边界**：传输 API 自身不是计算收益证明；必须把 upload/download、布局转换和 fence/sync 纳入基准。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

### `vkQueueSubmit`

把命令缓冲及同步对象提交给 GPU queue。

队列提交划定 CPU-GPU 异步执行与同步边界；它对吞吐和延迟影响很大，但不是数据并行计算本身的证据。

- **暴露层级**：`third_party_sdk_header`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/third_party/vulkan-headers/vulkan/vulkan_core.h:4185`；库：Vulkan loader（目标镜像验证）；SystemCapability：not asserted by third-party header。
- **代码/AST 信号**：二维或三维独立循环；规则数组/图像下标访问；每像素或每元素独立写回；可提取为无跨迭代依赖的 kernel。
- **数据形态**：连续 buffer 或图像纹理；明确宽高/stride/format；可描述的输入输出边界。
- **Top-N 匹配别名**：fence, queue_submit, semaphore, vkqueuesubmit。
- **单独卸载边界**：必须先建立 shader、pipeline 和同步边界；小任务或频繁 CPU-GPU 往返通常不值得独立卸载。
- **直接 strict case**：无。
- **能力边界**：GPU SDK 头文件或系统源码中的 GPU 路径只证明接口/集成存在；不证明当前设备的驱动、extension、format、shader 或性能可用。
- **运行时验证**：
  1. 确认目标镜像导出所需 Vulkan/OpenGL ES 库，并创建可用 graphics/compute context、queue 和 surface。
  2. 查询并验证所需 API version、extension、shader language、纹理/buffer format、usage、alignment 和 NativeBuffer/DMA 互操作条件。
  3. 以真实输入尺寸、stride、format 构建 shader/pipeline 与命令缓冲；对 validation/creation/submit 错误保留 CPU fallback。
  4. 在包含纹理上传下载、buffer copy、layout transition、fence/sync、上下文切换和回退的端到端 workload 上 benchmark。

## 构建与校验

```bash
python3 docs_ffrt/tools/build_accelerator_api_knowledge_bases.py
python3 docs_ffrt/tools/build_accelerator_api_knowledge_bases.py --check
```

产物 SHA-256：`aff895dd2a2cb48e87a4a6e2ed8c98cc28d23081b6f63c0c5520a4abe4c51746`
