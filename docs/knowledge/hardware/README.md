# 硬件知识库

`hardware_knowledge_base.json` 是平台正式消费的**硬件单元知识库**。它把源码中可见的异构执行路径、软件接口、约束和关联严格案例聚合为硬件单元记录，并与优化知识库分开发布。

本目录还包含 NPU、GPU、DSP 和媒体硬件加速的版本化 API 子知识库。它们不替代硬件单元记录，也不把“源码接口存在”升级为“当前设备支持”。NPU 子库提取 NNRT/MindSpore 的图算子与调用入口；GPU、DSP、媒体子库分别提取图形 SDK/系统图像路径、Audio Effect HDI/VDI 合同，以及 AVCodec、HCodec Codec Component HDI、JPEG/HEIF Image Codec HDI 与系统图像编解码路径，用于把热点代码先检索为带明确运行时门禁的 Top-N 候选。

## 当前基线

| 项目 | 当前值 |
|---|---:|
| 硬件知识记录 | 22 |
| 严格案例聚合的源码集成单元 | 11 |
| 仅保留的历史源码能力记录 | 11 |
| 关联的严格异构 + CPU/software fallback 案例 | 40 |

严格案例聚合单元及其关联数量如下：

| 硬件单元 | 案例数 |
|---|---:|
| `GPU_OPENCL` | 1 |
| `GPU_SOURCE_PATH` | 4 |
| `GPU_OPENGL` | 2 |
| `JPEG_HW_ENCODER` | 2 |
| `JPEG_HW_DECODER` | 1 |
| `HEIF_HW_DECODER` | 3 |
| `HCODEC_HDI` | 1 |
| `VIDEO_DECODER_HARDWARE` | 3 |
| `AUDIO_EFFECT_DSP_HDI` | 1 |
| `KIRIN_NPU_HIAI_DDK` | 21 |
| `KIRIN_NPU_NNRT` | 1 |

## NPU API 子知识库

| 项目 | 当前值 |
|---|---:|
| NNRT 图算子 | 108 |
| 与 NPU/NNRT 部署相关的公开入口 | 38 |
| 关联的严格 NPU fallback 案例 | 22 |
| MindSpore 内部 HiAI delegate 使用符号 | 67 |

完整目录与每条 API/算子的中文摘要、源码注释、shape/dtype 约束、静态代码特征、严格案例关联和运行时门禁见 [NPU_API_KNOWLEDGE_BASE.md](NPU_API_KNOWLEDGE_BASE.md)。生成器是 [build_npu_api_knowledge_base.py](../build_npu_api_knowledge_base.py)。

其中 `OH_NN_OPS_*` 只证明 NNRT 能表达该图节点，**不**证明当前手机的 NPU 支持它。Top-N 命中也只是代码语义候选：必须用真实 shape/dtype/layout 构建完整模型，执行 `OH_NNModel_GetAvailableOperations`，再选择 device、编译并端到端 benchmark。MindSpore 内部的 `hiai::op::*` 仅作为源码集成证据；当前工作区未建立可供普通应用直接调用的公开 ABI 合同。

## GPU、DSP 与媒体 API 子知识库

| 子知识库 | API / 集成入口 | 同单元 strict fallback 案例 | 与具体入口直接关联 | 主要暴露层级 |
|---|---:|---:|---:|---|
| [GPU_API_KNOWLEDGE_BASE.md](GPU_API_KNOWLEDGE_BASE.md) | 18 | 7 | 5 | Vulkan/OpenGL ES header、OpenHarmony NativeBuffer extension、系统图像路径 |
| [DSP_API_KNOWLEDGE_BASE.md](DSP_API_KNOWLEDGE_BASE.md) | 9 | 1 | 4 | Audio Effect HDI/VDI 合同、系统音频路径、少量 NDK 高层控制 |
| [MEDIA_ACCELERATOR_API_KNOWLEDGE_BASE.md](MEDIA_ACCELERATOR_API_KNOWLEDGE_BASE.md) | 31 | 10 | 18 | AVCodec NDK、HCodec Codec Component HDI、JPEG/HEIF Image Codec HDI 与系统媒体/图像路径 |

三份 JSON 目录及其统一 manifest 为：`gpu_api_knowledge_base.json`、`dsp_api_knowledge_base.json`、`media_accelerator_api_knowledge_base.json` 与 `accelerator_api_knowledge_manifest.json`。它们由 [build_accelerator_api_knowledge_bases.py](../build_accelerator_api_knowledge_bases.py) 从当前工作区源码生成，并对每条记录保存源码文件、行号、文件 SHA、语义族、静态信号、数据形态、暴露层级、严格案例关联与单元专属运行时协议。

- **GPU**：Vulkan/OpenGL ES 头文件可证明 SDK 表面；`GpuBrightnessAlgo`、`PixelMapPostProcWithGL`、ASTC 等系统路径只能作为源码集成参考。必须验证 context、extension、shader、format、buffer 生命周期、上传下载与 fence/sync。
- **DSP**：`IEffectControlVdi` 与 `EffectFactory` 是驱动/系统合同，不是普通应用 DSP kernel API。必须验证实际 provider、PCM sample format、channel、frameCount、实时延迟、bypass 和 AP effect chain 回退。
- **媒体**：公开视频 codec 首先经过 `OH_AVCodec_GetCapabilityByCategory`；HCodec 需由 `ICodecComponentManager` 返回真实 component capability；JPEG/HEIF Image Codec 路径还需验证 `ICodecImage` 服务、bitstream、Ashmem/DMA/NativeBuffer、格式和色彩空间。`ExtEncoder::DoHardWareEncode` 是内部动态委派而非公开 JPEG 编码 SDK；所有路径都必须保留 software fallback。

加速器 API 检索只在可见代码出现对应正向领域信号时返回候选：GPU 需要图像/像素/texture 与颜色、几何或 compute 信号；DSP 需要 PCM/sample/frame 与 effect 语义；媒体需要视频 codec 生命周期、JPEG/HEIF 图像编解码或 HCodec/Codec Component HDI 信号。普通 IPC、锁等待、通用循环或 `buffer` 名称必须返回空候选。

## 证据边界

所有记录都固定为 `device_availability=evidence_required` 与 `performance_envelope=not_provided`。这意味着当前库只证实：源码存在某种集成入口，以及列出的严格案例存在 CPU/software fallback 关系。

在对新热点提出可实施建议前，必须额外取得目标设备的 capability probe、驱动/API 可用性、输入格式/shape/dtype/buffer 条件、同步与数据搬运路径，以及同一 workload 的 benchmark 证据。任何模型回答都不能替代这些证据。

## 构建与校验

硬件知识库和优化知识库由同一个可复现构建器一起生成：

```bash
python3 docs_ffrt/tools/build_separated_knowledge_bases.py
python3 docs_ffrt/tools/build_separated_knowledge_bases.py --check
python3 docs_ffrt/tools/build_npu_api_knowledge_base.py
python3 docs_ffrt/tools/build_npu_api_knowledge_base.py --check
python3 docs_ffrt/tools/build_accelerator_api_knowledge_bases.py
python3 docs_ffrt/tools/build_accelerator_api_knowledge_bases.py --check
```

硬件单元构建器要求每个优化知识库 `target_unit` 都存在对应硬件记录，并将输入审计文件 SHA-256 写入 `input_provenance`。NPU API 构建器对 108 个公开 NNRT 算子做完整性校验，并在严格 case 与 NNRT 图算子不是精确同义时拒绝强行关联。GPU/DSP/媒体构建器拒绝缺失的源码契约，并把“直接 strict case 关联”与“同单元案例库存”分开保存。历史 `hardware_capabilities.json` 仅作为硬件单元构建输入，正式运行时分别消费本目录的硬件单元产物和 API 子目录。
