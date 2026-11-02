# OpenHarmony 用户态与三方库严格异构到 CPU/software Fallback 总览

## 1. 范围与结论

本文收录 OpenHarmony 用户态、Foundation、ArkUI 与三方库的严格异构到 CPU/software fallback pair。当前共 **40 条**：v21 基线 17 条、Audio DSP v37 增量 1 条、MindSpore Lite NPU/NNRT v38 增量 22 条。

其中 **29 条**定位到唯一 CPU 函数定义并已提取 Tree-sitter AST；其余 **11 条**是 inline continuation、factory、runtime provider、外部库或保留子图。后者不产生伪造的算法 AST，而是记录真实控制流及其后续解析门禁。所有精确函数当前仍处于 `compute_core=evidence_required`；“源码 AST 可读”不能直接转化为异构优化建议。Audio Effect 另保留一份 dispatcher AST 作为链路证据，但它不计入算法函数 profile。

## 2. 严格准入与 AST 方法

每条 pair 必须有异构实现、明确回退条件、CPU/software continuation 与跨文件源码证据。函数级 AST 仅在唯一生产定义可定位时提取：按全限定符定位定义，切取精确函数范围，对未知条件编译选择稳定的首分支，再用 `tree_sitter_cpp` 遍历函数子树。特征包含参数/局部类型、AST 节点数、循环/分支、下标/指针/赋值/内存调用、调用点、OpenMP、标签和 fingerprint。它不编译代码，也不解析函数指针、虚调用、动态 provider 或外部库；这些路径必须由 trace、runtime descriptor 或调用图继续下钻。

可复现的单函数导出命令：

```bash
python3 docs_ffrt/tools/extract_bound_function_ast_features.py \
  --workspace-root . \
  --file <relative-source-file> \
  --symbol <fully-qualified-function> \
  --out <ast-evidence.json>
```

## 3. 40 条严格 pair 索引

| 编号 | 异构单元 | 算法 | CPU/software 回退 | AST 绑定 |
| ---: | --- | --- | --- | --- |
| 1 | OpenCL | `astc_texture_encode` | `AstcCodec::AstcSoftwareEncodeCore` | `exact_definition` |
| 2 | GPU | `image_brightness_adjustment` | `CpuBrightnessAlgo::OnApplyRGBA8888` | `exact_definition` |
| 3 | JPEG 硬件编码器 | `camera_picture_jpeg_encode` | `EncodeJpegPicture` | `exact_definition` |
| 4 | DSP（Audio Effect HDI） | `audio_effect_processing` | `AudioEffectChain::ApplyEffectChain -> AudioEffectHandle::process` | `runtime_selected_provider` |
| 5 | GPU | `image_contrast_adjustment` | `CpuContrastAlgo::OnApplyRGBA8888` | `exact_definition` |
| 6 | HCodec HDI 硬件视频编解码器 | `video_codec_create_by_mime` | `FCodecLoader::CreateByName` | `runtime_selected_backend` |
| 7 | GPU | `screenshot_crop_and_scale` | `GetScreenshotWithOption plus Media::PixelMap::Create` | `inline_dispatch_region` |
| 8 | HEIF 硬件解码器 | `heif_alpha_composition` | `SwApplyAlphaImage` | `exact_definition` |
| 9 | HEIF 硬件解码器 | `heif_auxiliary_image_decode` | `SwDecodeAuxiliaryImage` | `exact_definition` |
| 10 | HEIF 硬件解码器 | `heif_primary_image_decode` | `SwDecode` | `exact_definition` |
| 11 | GPU | `image_texture_compression` | `RSImage::BuildFromBitmap` | `external_or_unresolved_definition` |
| 12 | JPEG 硬件解码器 | `jpeg_decode` | `SkCodec::getPixels` | `external_or_unresolved_definition` |
| 13 | JPEG 硬件编码器 | `jpeg_encode` | `ExtEncoder::EncodeImageByBitmap via DoEncode` | `exact_definition` |
| 14 | OpenGL GPU | `image_rotate` | `PixelMap::rotate` | `exact_definition` |
| 15 | OpenGL GPU | `image_scale` | `PostProc::ScalePixelMapEx` | `exact_definition` |
| 16 | 硬件视频解码器 | `video_thumbnail_decode` | `AVThumbnailGenerator::InitDecoder with AVCODEC_SOFTWARE` | `runtime_selected_backend` |
| 17 | 硬件视频解码器 | `video_decode` | `DecoderSurfaceFilter::CheckDecoderCapability(false) and VideoDecoderAdapter::Init` | `runtime_selected_backend` |
| 18 | 硬件视频解码器 | `video_decode` | `OH_AVCodec_GetCapabilityByCategory(..., SOFTWARE) plus OH_VideoDecoder_CreateByName` | `runtime_selected_backend` |
| 19 | Kirin NPU（HiAI DDK） | `mindspore_add_fusion` | `ArithmeticCPUKernel::Run` | `exact_definition` |
| 20 | Kirin NPU（HiAI DDK） | `mindspore_broadcast_to` | `BroadcastToCPUKernel::Run` | `exact_definition` |
| 21 | Kirin NPU（HiAI DDK） | `mindspore_cast` | `CastCPUKernel::Run` | `exact_definition` |
| 22 | Kirin NPU（HiAI DDK） | `mindspore_conv2d_fusion` | `CpuConvFp32KernelCreator` | `runtime_selected_cpu_kernel` |
| 23 | Kirin NPU（HiAI DDK） | `mindspore_conv2d_transpose_fusion` | `CpuDeConvFp32KernelCreator` | `runtime_selected_cpu_kernel` |
| 24 | Kirin NPU（HiAI DDK） | `mindspore_div_fusion` | `ArithmeticCPUKernel::Run` | `exact_definition` |
| 25 | Kirin NPU（HiAI DDK） | `mindspore_eltwise` | `ArithmeticCPUKernel::Run` | `exact_definition` |
| 26 | Kirin NPU（HiAI DDK） | `mindspore_floor_div` | `ArithmeticCPUKernel::Run` | `exact_definition` |
| 27 | Kirin NPU（HiAI DDK） | `mindspore_floor_mod` | `ArithmeticCPUKernel::Run` | `exact_definition` |
| 28 | Kirin NPU（HiAI DDK） | `mindspore_full_connection` | `FullconnectionFP16CPUKernel::Run` | `exact_definition` |
| 29 | Kirin NPU（HiAI DDK） | `mindspore_instance_norm` | `InstanceNormCPUKernel::Run` | `exact_definition` |
| 30 | Kirin NPU（HiAI DDK） | `mindspore_logical_and` | `ArithmeticCPUKernel::Run` | `exact_definition` |
| 31 | Kirin NPU（HiAI DDK） | `mindspore_logical_or` | `ArithmeticCPUKernel::Run` | `exact_definition` |
| 32 | Kirin NPU（HiAI DDK） | `mindspore_mat_mul_fusion` | `MatmulFP16CPUKernel::Run` | `exact_definition` |
| 33 | Kirin NPU（HiAI DDK） | `mindspore_maximum` | `ArithmeticCPUKernel::Run` | `exact_definition` |
| 34 | Kirin NPU（HiAI DDK） | `mindspore_minimum` | `ArithmeticCPUKernel::Run` | `exact_definition` |
| 35 | Kirin NPU（HiAI DDK） | `mindspore_mul_fusion` | `ArithmeticCPUKernel::Run` | `exact_definition` |
| 36 | Kirin NPU（HiAI DDK） | `mindspore_resize` | `ResizeCPUKernel::Run` | `exact_definition` |
| 37 | Kirin NPU（HiAI DDK） | `mindspore_split` | `SplitBaseCPUKernel::Run` | `exact_definition` |
| 38 | Kirin NPU（HiAI DDK） | `mindspore_sub_fusion` | `ArithmeticCPUKernel::Run` | `exact_definition` |
| 39 | Kirin NPU（HiAI DDK） | `mindspore_unstack` | `UnstackCPUKernel::Run` | `exact_definition` |
| 40 | Kirin NPU（OpenHarmony NNRT） | `inference_subgraph` | `original CPU KernelExec retained by Scheduler::ReplaceDelegateKernels` | `runtime_selected_original_cpu_kernel` |

## 4. 证据状态约定

`exact_definition` 表示唯一源码函数范围和语法 AST 已获取；它仍需 `compute_core` 审计。`inline_dispatch_region`、`runtime_selected_*` 与 `external_or_unresolved_definition` 表示本轮不能安全产出唯一 CPU 算法 AST。

## 5. 全量案例：CPU 代码、AST 特征与异构单元映射

### 5.1 OpenCL：astc_texture_encode

**严格 pair**：`astc_opencl_encode_to_cpu_software_fallback`。

- **异构单元**：OpenCL，实现路径为 `AstcCodec::TryAstcEncBasedOnCl`。
- **CPU/software 回退**：`AstcCodec::AstcSoftwareEncodeCore`；当前绑定状态 `exact_definition`。
- **代码语义**：CPU 入口配置 ASTC 软件编码后调用 astcenc；真正的主要计算在 encoder callee，不在当前 wrapper。

**CPU 代码（精确函数定义）**：[openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/texture_encode/astc_codec.cpp:437](../openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/texture_encode/astc_codec.cpp:437)。

```cpp
   437  bool AstcCodec::AstcSoftwareEncodeCore(TextureEncodeOptions &param, uint8_t *pixmapIn, uint8_t *astcBuffer)
   438  {
   439      if ((pixmapIn == nullptr) || (astcBuffer == nullptr)) {
   440          IMAGE_LOGE("pixmapIn or astcBuffer is nullptr");
   441          return false;
   442      }
   443      if (param.astcBytes < TEXTURE_HEAD_BYTES || param.width_ <= 0 || param.height_ <= 0) {
   444          IMAGE_LOGE("AstcSoftwareEncodeCore invalid param astcBytes %{public}d, w %{public}d, h %{public}d!",
   445                     param.astcBytes, param.width_, param.height_);
   446          return false;
   447      }
   448      AstcEncoder work;
   449      if (!InitMem(&work, param)) {
   450          FreeMem(&work);
   451          return false;
   452      }
   453      if (InitAstcencConfig(&work, &param) != SUCCESS) {
   454          IMAGE_LOGE("astc InitAstcencConfig failed");
   455          FreeMem(&work);
   456          return false;
   457      }
   458      work.image_.data[0] = pixmapIn;
   459      work.data_out_ = astcBuffer;
   460      if (GenAstcHeader(work.data_out_, work.image_, param) != SUCCESS) {
   461          IMAGE_LOGE("astc GenAstcHeader failed");
   462          FreeMem(&work);
   463          return false;
   464      }
   465      work.error_ = astcenc_compress_image(work.codec_context, &work.image_, &work.swizzle_,
   466          work.data_out_ + TEXTURE_HEAD_BYTES, param.astcBytes - TEXTURE_HEAD_BYTES,
   467  #if defined(QUALITY_CONTROL) && (QUALITY_CONTROL == 1)
   468          work.calQualityEnable, work.mse,
   469  #endif
   470          0);
   471  #if defined(QUALITY_CONTROL) && (QUALITY_CONTROL == 1)
   472      if ((ASTCENC_SUCCESS != work.error_) ||
   473          (work.calQualityEnable && !CheckQuality(work.mse, param.blocksNum, param.blockX_ * param.blockY_))) {
   474  #else
   475      if (ASTCENC_SUCCESS != work.error_) {
   476  #endif
   477          IMAGE_LOGE("astc compress failed");
   478          FreeMem(&work);
   479          return false;
   480      }
   481      FreeMem(&work);
   482      return true;
   483  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `AstcCodec::AstcSoftwareEncodeCore`；`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/texture_encode/astc_codec.cpp:437` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；47 行，59 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | TextureEncodeOptions &param，uint8_t *pixmapIn，uint8_t *astcBuffer；局部类型：AstcEncoder | 数据类型与接口边界 |
| 控制流 | 循环：无；if=6，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=1，指针=10，赋值=3，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `CheckQuality` x1 (L473)；`FreeMem` x5 (L450,455,462,478,481)；`GenAstcHeader` x1 (L460)；`IMAGE_LOGE` x4 (L440,454,461,477)；`IMAGE_LOGE` x1 (L444)；`InitAstcencConfig` x1 (L453)；`InitMem` x1 (L449)；`astcenc_compress_image` x1 (L465) | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access`, `compress`, `encode` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `ef5a2aaf4bb9814fa2069f937f6f0d7c91fffd406a365dcc583b2b70dcf12e0d`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：`OpenCL` 与 CPU fallback 的路径由源码确认。是否值得卸载仍取决于数据规模、格式支持、CPU-GPU 传输/同步开销和真实 trace 热点。

### 5.2 GPU：image_brightness_adjustment

**严格 pair**：`efilter_brightness_gpu_cpu_mapping`。

- **异构单元**：GPU，实现路径为 `gpuBrightnessAlgo_->OnApplyRGBA8888`。
- **CPU/software 回退**：`CpuBrightnessAlgo::OnApplyRGBA8888`；当前绑定状态 `exact_definition`。
- **代码语义**：CPU 函数显式执行 RGBA LUT：图像足够大时，逐像素通道读写是主要负载。

**CPU 代码（精确函数定义）**：[openharmony/foundation/multimedia/image_effect/frameworks/native/efilter/filterimpl/brightness/cpu_brightness_algo.cpp:61](../openharmony/foundation/multimedia/image_effect/frameworks/native/efilter/filterimpl/brightness/cpu_brightness_algo.cpp:61)。

```cpp
    61  ErrorCode CpuBrightnessAlgo::OnApplyRGBA8888(EffectBuffer *src, EffectBuffer *dst,
    62      std::map<std::string, Any> &value, std::shared_ptr<EffectContext> &context)
    63  {
    64      EFFECT_LOGI("CpuBrightnessAlgo::OnApplyRGBA8888 enter!");
    65      CHECK_AND_RETURN_RET_LOG(src != nullptr && dst != nullptr, ErrorCode::ERR_INPUT_NULL, "input para is null!");
    66      float brightness = ParseBrightness(value);
    67      auto *srcRgb = static_cast<unsigned char *>(src->buffer_);
    68      auto *dstRgb = static_cast<unsigned char *>(dst->buffer_);
    69  
    70      uint32_t width = src->bufferInfo_->width_;
    71      uint32_t height = src->bufferInfo_->height_;
    72  
    73      if (BrightnessCheckBufferInfolen(src, dst, width, height) != ErrorCode::SUCCESS) {
    74          return ErrorCode::ERR_INVALID_PARAMETER_VALUE;
    75      }
    76  
    77      float eps = ESP;
    78      if (fabs(brightness) < eps) {
    79          if (src != dst) {
    80              errno_t result = memcpy_s(dstRgb, dst->bufferInfo_->len_, srcRgb, src->bufferInfo_->len_);
    81              CHECK_AND_RETURN_RET_LOG(result == 0, ErrorCode::ERR_MEMCPY_FAIL, "memory copy failed: %{public}d", result);
    82          }
    83          return ErrorCode::SUCCESS;
    84      }
    85      float scale = brightness / SCALE_FACTOR;
    86      scale = pow(2.4f, scale); // 2.4 is algorithm parameter.
    87  
    88      unsigned char lut[UNSIGHED_CHAR_DATA_RECORDS] = {0};
    89      for (uint32_t idx = 0; idx < UNSIGHED_CHAR_DATA_RECORDS; idx++) {
    90          float current = CommonUtils::Clip(1.f - (float)(idx) / UNSIGHED_CHAR_MAX, 0, 1) + eps;
    91          current = 1.f - pow(current, scale);
    92          current = CommonUtils::Clip(current, 0, 1);
    93          lut[idx] = (unsigned char)(current * UNSIGHED_CHAR_MAX);
    94      }
    95  
    96      uint32_t srcRowStride = src->bufferInfo_->rowStride_;
    97      uint32_t dstRowStride = dst->bufferInfo_->rowStride_;
    98      
    99      if (srcRowStride * (height - 1) + (width - 1) * BYTES_PER_INT + BYTES_PER_INT >  dst->bufferInfo_->len_ ||
   100      dstRowStride * (height - 1) + (width - 1) * BYTES_PER_INT + BYTES_PER_INT > src->bufferInfo_->len_) {
   101          return ErrorCode::ERR_INVALID_PARAMETER_VALUE;
   102      }
   103  #pragma omp parallel for default(none) shared(height, width, dstRgb, srcRgb, lut, srcRowStride, dstRowStride)
   104      for (uint32_t y = 0; y < height; ++y) {
   105          for (uint32_t x = 0; x < width; ++x) {
   106              for (uint32_t i = 0; i < BYTES_PER_INT; ++i) {
   107                  uint32_t srcIndex = srcRowStride * y + x * BYTES_PER_INT + i;
   108                  uint32_t dstIndex = dstRowStride * y + x * BYTES_PER_INT + i;
   109                  dstRgb[dstIndex] = (i == RGBA_ALPHA_INDEX) ? srcRgb[srcIndex] : lut[srcRgb[srcIndex]];
   110              }
   111          }
   112      }
   113      return ErrorCode::SUCCESS;
   114  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `CpuBrightnessAlgo::OnApplyRGBA8888`；`openharmony/foundation/multimedia/image_effect/frameworks/native/efilter/filterimpl/brightness/cpu_brightness_algo.cpp:61` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；54 行，95 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | EffectBuffer *src，EffectBuffer *dst，std::map<std::string, Any> &value，std::shared_ptr<EffectContext> &context；局部类型：errno_t, float, uint32_t, unsigned char | 数据类型与接口边界 |
| 控制流 | 循环：for_statement@L89 depth=1；for_statement@L104 depth=1；for_statement@L105 depth=2；for_statement@L106 depth=3；if=4，switch=0，ternary=1 | 区分调度循环与数据并行循环 |
| 访存 | 下标=5，指针=0，赋值=5，内存库调用=1 | 只描述本函数体可见的访存 |
| 调用 | `BrightnessCheckBufferInfolen` x1 (L73)；`CHECK_AND_RETURN_RET_LOG` x1 (L65)；`CHECK_AND_RETURN_RET_LOG` x1 (L81)；`CommonUtils::Clip` x2 (L90,92)；`EFFECT_LOGI` x1 (L64)；`ParseBrightness` x1 (L66)；`fabs` x1 (L78)；`memcpy_s` x1 (L80)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=1；`apply`, `buffer_access`, `explicit_parallel_loop`, `iterative`, `memory_copy_or_fill` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `2a9da8c4c2ad973d60f972b47e8c83e91d8c25b169ac9f1d32aabce37bc4ae7d`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：`GPU` 与 CPU fallback 的路径由源码确认。是否值得卸载仍取决于数据规模、格式支持、CPU-GPU 传输/同步开销和真实 trace 热点。

### 5.3 JPEG 硬件编码器：camera_picture_jpeg_encode

**严格 pair**：`jpeg_picture_hardware_encode_to_software_fallback`。

- **异构单元**：JPEG 硬件编码器，实现路径为 `TryHardwareEncodePicture`。
- **CPU/software 回退**：`EncodeJpegPicture`；当前绑定状态 `exact_definition`。
- **代码语义**：CPU 函数按动态范围分派到软件 JPEG 路径，DCT、量化与熵编码需继续解析 callee。

**CPU 代码（精确函数定义）**：[openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_encoder.cpp:2599](../openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_encoder.cpp:2599)。

```cpp
  2599  uint32_t ExtEncoder::EncodeJpegPicture(SkWStream& skStream)
  2600  {
  2601      uint32_t error = ERR_IMAGE_ENCODE_FAILED;
  2602      switch (opts_.desiredDynamicRange) {
  2603          case EncodeDynamicRange::AUTO:
  2604              error = EncodeJpegPictureDualVivid(skStream);
  2605              if (error != SUCCESS) {
  2606                  IMAGE_LOGI("%{public}s jpeg picture encode dual vivid failed, try encode sdr", __func__);
  2607                  error = EncodeJpegPictureSdr(skStream);
  2608              }
  2609              break;
  2610          case EncodeDynamicRange::SDR:
  2611              error = EncodeJpegPictureSdr(skStream);
  2612              break;
  2613          case EncodeDynamicRange::HDR_VIVID_DUAL:
  2614              error = EncodeJpegPictureDualVivid(skStream);
  2615              break;
  2616          case EncodeDynamicRange::HDR_VIVID_SINGLE:
  2617              error = ERR_IMAGE_DECODE_FAILED;
  2618              break;
  2619          default:
  2620              error = ERR_IMAGE_INVALID_PARAMETER;
  2621              break;
  2622      }
  2623      return error;
  2624  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ExtEncoder::EncodeJpegPicture`；`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_encoder.cpp:2599` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；26 行，16 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | SkWStream& skStream；局部类型：uint32_t | 数据类型与接口边界 |
| 控制流 | 循环：无；if=1，switch=1，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=0，赋值=6，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `EncodeJpegPictureDualVivid` x2 (L2604,2614)；`EncodeJpegPictureSdr` x2 (L2607,2611)；`IMAGE_LOGI` x1 (L2606) | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`encode` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `28c1a96e8fee429dab30f54facae3c49720c7b660239ac2a7f56870512aa710e`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：`JPEG hardware encoder` 与 CPU/software continuation 的回退关系已被源码确认；具体设备能力、格式约束与实际收益必须由运行时和 trace 证据门控。

### 5.4 DSP（Audio Effect HDI）：audio_effect_processing

**严格 pair**：`audio_effect_dsp_hdi_to_ap_runtime_chain_fallback`。

- **异构单元**：DSP（Audio Effect HDI），实现路径为 `AudioEffectHdiParam::SetHdiCommand -> IEffectControl::SendCommand`。
- **CPU/software 回退**：`AudioEffectChain::ApplyEffectChain -> AudioEffectHandle::process`；当前绑定状态 `runtime_selected_provider`。
- **代码语义**：AP chain 将浮点帧 buffer 调度给运行时 effect callback；具体 effect 算法不在 dispatcher AST 内。

**AP effect-chain dispatcher 代码（精确绑定，但不是 provider 算法函数）**：[openharmony/foundation/multimedia/audio_framework/frameworks/native/audioeffect/src/audio_effect_chain.cpp:361](../openharmony/foundation/multimedia/audio_framework/frameworks/native/audioeffect/src/audio_effect_chain.cpp:361)。

```cpp
   361  void AudioEffectChain::ApplyEffectChain(float *bufIn, float *bufOut, uint32_t frameLen, AudioEffectProcInfo procInfo)
   362  {
   363      Trace trace("AudioEffectChain::ApplyEffectChain");
   364      size_t inTotlen = frameLen * ioBufferConfig_.inputCfg.channels * sizeof(float);
   365      size_t outTotlen = frameLen * ioBufferConfig_.outputCfg.channels * sizeof(float);
   366      DumpFileUtil::WriteDumpFile(dumpFileInput_, static_cast<void *>(bufIn), inTotlen);
   367      DumpEffectProcessData(dumpNameIn_, static_cast<void *>(bufIn), inTotlen);
   368  
   369      if (IsEmptyEffectHandles()) {
   370          CHECK_AND_RETURN_LOG(memcpy_s(bufOut, outTotlen, bufIn, outTotlen) == 0, "memcpy error in apply effect");
   371          DumpFileUtil::WriteDumpFile(dumpFileOutput_, static_cast<void *>(bufOut), outTotlen);
   372          return;
   373      }
   374  
   375  #ifdef SENSOR_ENABLE
   376      int32_t replyData = 0;
   377      auto imuData = headTracker_->GetHeadPostureData();
   378      AudioEffectTransInfo cmdInfo = {sizeof(HeadPostureData), &imuData};
   379      AudioEffectTransInfo replyInfo = {sizeof(int32_t), &replyData};
   380  #endif
   381  
   382      audioBufIn_.frameLength = frameLen;
   383      audioBufOut_.frameLength = frameLen;
   384      std::lock_guard<std::mutex> lock(reloadMutex_);
   385      for (size_t i = 0; i < standByEffectHandles_.size(); ++i) {
   386  #ifdef SENSOR_ENABLE
   387          if ((!procInfo.btOffloadEnabled) && procInfo.headTrackingEnabled) {
   388              (*standByEffectHandles_[i])->command(standByEffectHandles_[i], EFFECT_CMD_SET_IMU, &cmdInfo, &replyInfo);
   389          }
   390  #endif
   391          audioBufIn_.raw = i == 0 ? bufIn : effectBuffer_.data();
   392          audioBufOut_.raw = i == (standByEffectHandles_.size() - 1) ? bufOut : effectBuffer_.data();
   393  
   394          int32_t ret = (*standByEffectHandles_[i])->process(standByEffectHandles_[i], &audioBufIn_, &audioBufOut_);
   395          CHECK_AND_CONTINUE_LOG(ret == 0, "[%{public}s] with mode [%{public}s], either one of libs process fail",
   396              sceneType_.c_str(), effectMode_.c_str());
   397      }
   398  
   399      CrossFadeProcess(bufOut, frameLen);
   400  
   401      DumpFileUtil::WriteDumpFile(dumpFileOutput_, static_cast<void *>(bufOut), outTotlen);
   402  }
```

**dispatcher AST 特征（不进入算法匹配）**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `AudioEffectChain::ApplyEffectChain`；`openharmony/foundation/multimedia/audio_framework/frameworks/native/audioeffect/src/audio_effect_chain.cpp:361` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；42 行，65 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | float *bufIn，float *bufOut，uint32_t frameLen，AudioEffectProcInfo procInfo；局部类型：AudioEffectTransInfo, Trace, int32_t, size_t, std::lock_guard<std::mutex> | 数据类型与接口边界 |
| 控制流 | 循环：for_statement@L385 depth=1；if=2，switch=0，ternary=2 | 区分调度循环与数据并行循环 |
| 访存 | 下标=4，指针=8，赋值=4，内存库调用=1 | 只描述本函数体可见的访存 |
| 调用 | `(*standByEffectHandles_[i])->command` x1 (L388)；`(*standByEffectHandles_[i])->process` x1 (L394)；`CHECK_AND_CONTINUE_LOG` x1 (L395)；`CHECK_AND_RETURN_LOG` x1 (L370)；`CrossFadeProcess` x1 (L399)；`DumpEffectProcessData` x1 (L367)；`DumpFileUtil::WriteDumpFile` x3 (L366,371,401)；`IsEmptyEffectHandles` x1 (L369)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`apply`, `buffer_access`, `iterative`, `memory_copy_or_fill`, `process` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `6e09bc5ce7d5738a47eb9e72003e7672b78547984de89a6353cb18bfa4fd60d3`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

**CPU 回退控制流证据（非唯一算法函数）**：

```cpp
   390  #endif
   391          audioBufIn_.raw = i == 0 ? bufIn : effectBuffer_.data();
   392          audioBufOut_.raw = i == (standByEffectHandles_.size() - 1) ? bufOut : effectBuffer_.data();
   393  
   394          int32_t ret = (*standByEffectHandles_[i])->process(standByEffectHandles_[i], &audioBufIn_, &audioBufOut_);
   395          CHECK_AND_CONTINUE_LOG(ret == 0, "[%{public}s] with mode [%{public}s], either one of libs process fail",
   396              sceneType_.c_str(), effectMode_.c_str());
   397      }
   398  
   399      CrossFadeProcess(bufOut, frameLen);
   400  
   401      DumpFileUtil::WriteDumpFile(dumpFileOutput_, static_cast<void *>(bufOut), outTotlen);
   402  }
   403  
```

**AST / 绑定结论**：

| AST / 绑定项 | 结果 | 工程含义 |
| --- | --- | --- |
| 绑定状态 | `runtime_selected_provider` | provider/callback 在运行时选择；当前 dispatcher 不是具体算法函数体。 |
| 请求符号 | `AudioEffectChain::ApplyEffectChain`, `AudioEffectHandle::process` | 这些符号描述路径，不等于可安全分析的算法函数 |
| 控制流证据 | [openharmony/foundation/multimedia/audio_framework/frameworks/native/audioeffect/src/audio_effect_chain.cpp:394](../openharmony/foundation/multimedia/audio_framework/frameworks/native/audioeffect/src/audio_effect_chain.cpp:394) | 展示真实回退边界 |
| 候选定义 | 无唯一函数定义 | 候选、factory 或 dispatcher 不可替代实际 CPU 算法 AST |
| AST 结论 | 未生成函数级算法 AST | 必须结合 trace、runtime descriptor、provider/codec 解析或模型实例继续下钻 |

- **异构映射与边界**：DSP/HDI 路径与 AP 链的控制流关系已由源码确认。固定帧音频是候选负载，但只有实际 provider 的滤波/状态/向量化特征才能证明 DSP 适配性。

### 5.5 GPU：image_contrast_adjustment

**严格 pair**：`efilter_contrast_gpu_cpu_mapping`。

- **异构单元**：GPU，实现路径为 `gpuContrastAlgo_->OnApplyRGBA8888`。
- **CPU/software 回退**：`CpuContrastAlgo::OnApplyRGBA8888`；当前绑定状态 `exact_definition`。
- **代码语义**：RGBA 对比度调整具有规则的像素/LUT 访问，CPU 函数体可见连续图像 buffer 遍历。

**CPU 代码（精确函数定义）**：[openharmony/foundation/multimedia/image_effect/frameworks/native/efilter/filterimpl/contrast/cpu_contrast_algo.cpp:52](../openharmony/foundation/multimedia/image_effect/frameworks/native/efilter/filterimpl/contrast/cpu_contrast_algo.cpp:52)。

```cpp
    52  ErrorCode CpuContrastAlgo::OnApplyRGBA8888(EffectBuffer *src, EffectBuffer *dst,
    53      std::map<std::string, Any> &value, std::shared_ptr<EffectContext> &context)
    54  {
    55      EFFECT_LOGI("CpuContrastAlgo::OnApplyRGBA8888 enter!");
    56      CHECK_AND_RETURN_RET_LOG(src != nullptr && dst != nullptr, ErrorCode::ERR_INPUT_NULL, "input para is null!");
    57      float contrast = ParseContrast(value);
    58      auto *srcRgb = static_cast<unsigned char *>(src->buffer_);
    59      auto *dstRgb = static_cast<unsigned char *>(dst->buffer_);
    60  
    61      uint32_t width = src->bufferInfo_->width_;
    62      uint32_t height = src->bufferInfo_->height_;
    63  
    64      if (ContrastCheckBufferInfolen(src, dst, width, height) != ErrorCode::SUCCESS) {
    65          return ErrorCode::ERR_INVALID_PARAMETER_VALUE;
    66      }
    67  
    68      float eps = ESP;
    69      if (fabs(contrast) < eps) {
    70          if (src != dst) {
    71              errno_t result = memcpy_s(dstRgb, dst->bufferInfo_->len_, srcRgb, src->bufferInfo_->len_);
    72              CHECK_AND_RETURN_RET_LOG(result == 0, ErrorCode::ERR_MEMCPY_FAIL, "memory copy failed: %{public}d", result);
    73          }
    74          return ErrorCode::SUCCESS;
    75      }
    76      float scale = contrast / SCALE_FACTOR;
    77  
    78      unsigned char lut[UNSIGHED_CHAR_DATA_RECORDS] = {0};
    79      for (uint32_t idx = 0; idx < UNSIGHED_CHAR_DATA_RECORDS; idx++) {
    80          float current = (float)idx / UNSIGHED_CHAR_MAX;
    81          current = current - scale * 0.1f * sin(ALGORITHM_PARAMTER_FACTOR * PI * current);
    82          current = CommonUtils::Clip(current, 0, 1);
    83          lut[idx] = (unsigned char)(current * UNSIGHED_CHAR_MAX);
    84      }
    85  
    86      uint32_t srcRowStride = src->bufferInfo_->rowStride_;
    87      uint32_t dstRowStride = dst->bufferInfo_->rowStride_;
    88      
    89      if (srcRowStride * (height - 1) + (width - 1) * BYTES_PER_INT + BYTES_PER_INT >  dst->bufferInfo_->len_ ||
    90      dstRowStride * (height - 1) + (width - 1) * BYTES_PER_INT + BYTES_PER_INT > src->bufferInfo_->len_) {
    91          return ErrorCode::ERR_INVALID_PARAMETER_VALUE;
    92      }
    93  #pragma omp parallel for default(none) shared(height, width, dstRgb, srcRgb, lut, srcRowStride, dstRowStride)
    94      for (uint32_t y = 0; y < height; ++y) {
    95          for (uint32_t x = 0; x < width; ++x) {
    96              for (uint32_t i = 0; i < BYTES_PER_INT; ++i) {
    97                  uint32_t srcIndex = srcRowStride * y + x * BYTES_PER_INT + i;
    98                  uint32_t dstIndex = dstRowStride * y + x * BYTES_PER_INT + i;
    99                  dstRgb[dstIndex] = (i == RGBA_ALPHA_INDEX) ? srcRgb[srcIndex] : lut[srcRgb[srcIndex]];
   100              }
   101          }
   102      }
   103  
   104      return ErrorCode::SUCCESS;
   105  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `CpuContrastAlgo::OnApplyRGBA8888`；`openharmony/foundation/multimedia/image_effect/frameworks/native/efilter/filterimpl/contrast/cpu_contrast_algo.cpp:52` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；54 行，94 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | EffectBuffer *src，EffectBuffer *dst，std::map<std::string, Any> &value，std::shared_ptr<EffectContext> &context；局部类型：errno_t, float, uint32_t, unsigned char | 数据类型与接口边界 |
| 控制流 | 循环：for_statement@L79 depth=1；for_statement@L94 depth=1；for_statement@L95 depth=2；for_statement@L96 depth=3；if=4，switch=0，ternary=1 | 区分调度循环与数据并行循环 |
| 访存 | 下标=5，指针=0，赋值=4，内存库调用=1 | 只描述本函数体可见的访存 |
| 调用 | `CHECK_AND_RETURN_RET_LOG` x1 (L56)；`CHECK_AND_RETURN_RET_LOG` x1 (L72)；`CommonUtils::Clip` x1 (L82)；`ContrastCheckBufferInfolen` x1 (L64)；`EFFECT_LOGI` x1 (L55)；`ParseContrast` x1 (L57)；`fabs` x1 (L69)；`memcpy_s` x1 (L71)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=1；`apply`, `buffer_access`, `explicit_parallel_loop`, `iterative`, `memory_copy_or_fill` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `7a75fdfef3233cc2d8e1667916fa527ee042657aeb0d88555db65ddd7fbe9a4a`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：`GPU` 与 CPU fallback 的路径由源码确认。是否值得卸载仍取决于数据规模、格式支持、CPU-GPU 传输/同步开销和真实 trace 热点。

### 5.6 HCodec HDI 硬件视频编解码器：video_codec_create_by_mime

**严格 pair**：`avcodec_hcodec_to_fcodec_ordered_factory_fallback`。

- **异构单元**：HCodec HDI 硬件视频编解码器，实现路径为 `HCodecLoader::CreateByName via CodecServer::InitByMime`。
- **CPU/software 回退**：`FCodecLoader::CreateByName`；当前绑定状态 `runtime_selected_backend`。
- **代码语义**：CPU 回退是按顺序选择软件 codec factory，不是视频编解码算法内核。

**CPU 回退控制流证据（非唯一算法函数）**：

```cpp
    70          case CodecType::AVCODEC_HCODEC:
    71              codec = HCodecLoader::CreateByName(name);
    72              break;
    73          case CodecType::AVCODEC_VIDEO_CODEC:
    74              codec = FCodecLoader::CreateByName(name);
    75              break;
    76          case CodecType::AVCODEC_VIDEO_HEVC_DECODER:
    77              codec = HevcDecoderLoader::CreateByName(name);
    78              break;
    79          case CodecType::AVCODEC_VIDEO_AVC_ENCODER:
    80              codec = AvcEncoderLoader::CreateByName(name);
    81              break;
    82  #ifdef SUPPORT_CODEC_AV1
    83          case CodecType::AVCODEC_VIDEO_AV1_DECODER:
```

**AST / 绑定结论**：

| AST / 绑定项 | 结果 | 工程含义 |
| --- | --- | --- |
| 绑定状态 | `runtime_selected_backend` | 软件 backend 在运行时选择；factory/capability 代码不是编解码算法函数。 |
| 请求符号 | `FCodecLoader::CreateByName` | 这些符号描述路径，不等于可安全分析的算法函数 |
| 控制流证据 | [openharmony/foundation/multimedia/av_codec/services/services/codec/server/video/codec_factory.cpp:74](../openharmony/foundation/multimedia/av_codec/services/services/codec/server/video/codec_factory.cpp:74) | 展示真实回退边界 |
| 候选定义 | 无唯一函数定义 | 候选、factory 或 dispatcher 不可替代实际 CPU 算法 AST |
| AST 结论 | 未生成函数级算法 AST | 必须结合 trace、runtime descriptor、provider/codec 解析或模型实例继续下钻 |

- **异构映射与边界**：`HCodec HDI hardware video codec` 与 CPU/software continuation 的回退关系已被源码确认；具体设备能力、格式约束与实际收益必须由运行时和 trace 证据门控。

### 5.7 GPU：screenshot_crop_and_scale

**严格 pair**：`display_screenshot_gpu_to_cpu_fallback`。

- **异构单元**：GPU，实现路径为 `DisplayManager::GetScreenshotWithOptionUseGpu`。
- **CPU/software 回退**：`GetScreenshotWithOption plus Media::PixelMap::Create`；当前绑定状态 `inline_dispatch_region`。
- **代码语义**：CPU continuation 是内联的 screenshot/pixel-map 构造区域；裁剪和缩放计算需在更下层绑定。

**CPU 回退控制流证据（非唯一算法函数）**：

```cpp
  2929              return screenShot;
  2930          }
  2931          TLOGI(WmsLogTag::DMS, "set snapshot with option by gpu failed, try to use cpu!");
  2932      }
  2933      screenShot = GetScreenshotWithOption(captureOption, errorCode);
  2934      if (screenShot == nullptr) {
  2935          TLOGE(WmsLogTag::DMS, "set snapshot with option failed!");
  2936          return nullptr;
  2937      }
  2938      // create crop dest pixelmap
  2939      Media::InitializationOptions opt;
  2940      opt.size.width = size.width;
  2941      opt.size.height = size.height;
  2942      opt.scaleMode = Media::ScaleMode::FIT_TARGET_SIZE;
```

**AST / 绑定结论**：

| AST / 绑定项 | 结果 | 工程含义 |
| --- | --- | --- |
| 绑定状态 | `inline_dispatch_region` | CPU 工作位于 dispatcher 的内联 continuation 中，不是独立绑定的算法函数。 |
| 请求符号 | `Media::PixelMap::Create`, `GetScreenshotWithOption` | 这些符号描述路径，不等于可安全分析的算法函数 |
| 控制流证据 | [openharmony/foundation/window/window_manager/dm/src/display_manager.cpp:2933](../openharmony/foundation/window/window_manager/dm/src/display_manager.cpp:2933) | 展示真实回退边界 |
| 候选定义 | `?` (?)；`?` (?) | 候选、factory 或 dispatcher 不可替代实际 CPU 算法 AST |
| AST 结论 | 未生成函数级算法 AST | 必须结合 trace、runtime descriptor、provider/codec 解析或模型实例继续下钻 |

- **异构映射与边界**：`GPU` 与 CPU fallback 的路径由源码确认。是否值得卸载仍取决于数据规模、格式支持、CPU-GPU 传输/同步开销和真实 trace 热点。

### 5.8 HEIF 硬件解码器：heif_alpha_composition

**严格 pair**：`heif_hardware_alpha_to_software_alpha_fallback`。

- **异构单元**：HEIF 硬件解码器，实现路径为 `HwApplyAlphaImage`。
- **CPU/software 回退**：`SwApplyAlphaImage`；当前绑定状态 `exact_definition`。
- **代码语义**：软件 HEIF alpha 路径已绑定，但像素解码/合成工作委托给 image-codec 调用。

**CPU 代码（精确函数定义）**：[openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp:1213](../openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp:1213)。

```cpp
  1213  bool HeifDecoderImpl::SwApplyAlphaImage(std::shared_ptr<HeifImage> &masterImage,
  1214                                          uint8_t *dstMemory, size_t dstRowStride)
  1215  {
  1216      // check alpha image is available
  1217      CHECK_ERROR_RETURN_RET(!masterImage || IsDirectYUVDecode(), false);
  1218      std::shared_ptr<HeifImage> alphaImage = masterImage->GetAlphaImage();
  1219      if (!IsValidAlphaImage(masterImage, alphaImage, outPixelFormat_, false)) {
  1220          return false;
  1221      }
  1222  
  1223      GridInfo alphaGridInfo;
  1224      InitGridInfo(alphaImage, alphaGridInfo);
  1225      uint32_t tileSpanX = 0;
  1226      uint32_t tileSpanY = 0;
  1227      if (__builtin_mul_overflow(alphaGridInfo.cols, alphaGridInfo.tileWidth, &tileSpanX) ||
  1228          __builtin_mul_overflow(alphaGridInfo.rows, alphaGridInfo.tileHeight, &tileSpanY)) {
  1229          IMAGE_LOGE("Alpha image grid dimensions overflow: cols=%{public}u, tileWidth=%{public}u, "
  1230                     "rows=%{public}u, tileHeight=%{public}u",
  1231                     alphaGridInfo.cols, alphaGridInfo.tileWidth, alphaGridInfo.rows, alphaGridInfo.tileHeight);
  1232          return false;
  1233      }
  1234      uint32_t alphaStride = (alphaGridInfo.displayWidth > tileSpanX)
  1235          ? alphaGridInfo.displayWidth : tileSpanX;
  1236      uint32_t alphaHeight = (alphaGridInfo.displayHeight > tileSpanY)
  1237          ? alphaGridInfo.displayHeight : tileSpanY;
  1238      uint32_t alphaMemorySize = 0;
  1239      if (__builtin_mul_overflow(alphaStride, alphaHeight, &alphaMemorySize) || alphaMemorySize == 0) {
  1240          IMAGE_LOGE("Alpha image dimensions overflow: stride=%{public}u, height=%{public}u",
  1241                     alphaStride, alphaHeight);
  1242          return false;
  1243      }
  1244      PixelFormat alphaDstFmt = PixelFormat::ALPHA_8;
  1245      std::unique_ptr<uint8_t[]> alphaMemory = std::make_unique<uint8_t[]>(alphaMemorySize);
  1246      HevcSoftDecodeParam param {
  1247          alphaGridInfo, Media::PixelFormat::UNKNOWN, alphaDstFmt,
  1248          alphaMemory.get(), alphaMemorySize, alphaStride, nullptr, false, nullptr, alphaStride
  1249      };
  1250      bool decodeRes = SwDecodeImage(alphaImage, param, alphaGridInfo, false);
  1251      bool cond = !decodeRes;
  1252      CHECK_ERROR_RETURN_RET(cond, false);
  1253  
  1254      // merge alpha channel
  1255      return FillAlphaChannel(masterImage, alphaMemory.get(), alphaStride, dstMemory, dstRowStride);
  1256  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `HeifDecoderImpl::SwApplyAlphaImage`；`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp:1213` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；44 行，46 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | std::shared_ptr<HeifImage> &masterImage，uint8_t *dstMemory，size_t dstRowStride；局部类型：GridInfo, HevcSoftDecodeParam, PixelFormat, bool, std::shared_ptr<HeifImage>, std::unique_ptr<uint8_t[]>, uint32_t | 数据类型与接口边界 |
| 控制流 | 循环：无；if=3，switch=0，ternary=2 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=3，赋值=0，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `CHECK_ERROR_RETURN_RET` x2 (L1217,1252)；`FillAlphaChannel` x1 (L1255)；`IMAGE_LOGE` x1 (L1240)；`IMAGE_LOGE` x1 (L1229)；`InitGridInfo` x1 (L1224)；`IsDirectYUVDecode` x1 (L1217)；`IsValidAlphaImage` x1 (L1219)；`SwDecodeImage` x1 (L1250)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`apply`, `buffer_access`, `decode` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `d0b1a3634a468d6eb562bc075351297eee7ceb1cbd6a002516468fc512a72607`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：`HEIF hardware decoder` 与 CPU/software continuation 的回退关系已被源码确认；具体设备能力、格式约束与实际收益必须由运行时和 trace 证据门控。

### 5.9 HEIF 硬件解码器：heif_auxiliary_image_decode

**严格 pair**：`heif_hardware_auxiliary_decode_to_software_fallback`。

- **异构单元**：HEIF 硬件解码器，实现路径为 `HwDecodeImage`。
- **CPU/software 回退**：`SwDecodeAuxiliaryImage`；当前绑定状态 `exact_definition`。
- **代码语义**：软件辅助图解码入口已绑定；真正的解码核心位于 codec 实现调用中。

**CPU 代码（精确函数定义）**：[openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp:1038](../openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp:1038)。

```cpp
  1038  bool HeifDecoderImpl::SwDecodeAuxiliaryImage(std::shared_ptr<HeifImage> &gainmapImage,
  1039                                               GridInfo &gainmapGridInfo, uint8_t *auxiliaryDstMemory)
  1040  {
  1041      ImageTrace trace("HeifDecoderImpl::SwdecodeGainmap");
  1042      CHECK_ERROR_RETURN_RET(!gainmapImage, false);
  1043      uint32_t width = gainmapImage->GetOriginalWidth();
  1044      uint32_t height = gainmapImage->GetOriginalHeight();
  1045      sptr<SurfaceBuffer> output;
  1046      if (isGainmapDecode_ && gainMapDstHwbuffer_ != nullptr) {
  1047          output = sptr<SurfaceBuffer>(gainMapDstHwbuffer_);
  1048      } else if (isAuxiliaryDecode_ && auxiliaryDstHwbuffer_ != nullptr) {
  1049          output = sptr<SurfaceBuffer>(auxiliaryDstHwbuffer_);
  1050      } else {
  1051          output = SurfaceBuffer::Create();
  1052          CHECK_ERROR_RETURN_RET(!output, false);
  1053          BufferRequestConfig config = {
  1054              .width = width,
  1055              .height = height,
  1056              .format = GRAPHIC_PIXEL_FMT_YCBCR_420_SP,
  1057              .usage = BUFFER_USAGE_CPU_READ | BUFFER_USAGE_CPU_WRITE | BUFFER_USAGE_MEM_DMA | BUFFER_USAGE_MEM_MMZ_CACHE,
  1058              .timeout = 0
  1059          };
  1060          GSError ret = output->Alloc(config);
  1061          bool cond = ret != GSERROR_OK;
  1062          CHECK_ERROR_RETURN_RET_LOG(cond, false, "output->alloc(config)faild, GSError=%{public}d", ret);
  1063      }
  1064      if (!DoSwDecodeAuxiliaryImage(gainmapImage, gainmapGridInfo, output, auxiliaryDstMemory)) {
  1065          IMAGE_LOGE("HDR-IMAGE SwDecodeGainmap failed");
  1066          return false;
  1067      }
  1068      return true;
  1069  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `HeifDecoderImpl::SwDecodeAuxiliaryImage`；`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp:1038` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；32 行，34 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | std::shared_ptr<HeifImage> &gainmapImage，GridInfo &gainmapGridInfo，uint8_t *auxiliaryDstMemory；局部类型：BufferRequestConfig, GSError, ImageTrace, bool, sptr<SurfaceBuffer>, uint32_t | 数据类型与接口边界 |
| 控制流 | 循环：无；if=3，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=0，赋值=3，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `CHECK_ERROR_RETURN_RET` x2 (L1042,1052)；`CHECK_ERROR_RETURN_RET_LOG` x1 (L1062)；`DoSwDecodeAuxiliaryImage` x1 (L1064)；`IMAGE_LOGE` x1 (L1065)；`SurfaceBuffer::Create` x1 (L1051)；`gainmapImage->GetOriginalHeight` x1 (L1044)；`gainmapImage->GetOriginalWidth` x1 (L1043)；`output->Alloc` x1 (L1060)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`decode` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f6d5acb3fc10684e3d73a3b4f596630bee9a367ee087a41de29dbd583085bc4`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：`HEIF hardware decoder` 与 CPU/software continuation 的回退关系已被源码确认；具体设备能力、格式约束与实际收益必须由运行时和 trace 证据门控。

### 5.10 HEIF 硬件解码器：heif_primary_image_decode

**严格 pair**：`heif_hardware_primary_decode_to_software_fallback`。

- **异构单元**：HEIF 硬件解码器，实现路径为 `HwDecodeImage`。
- **CPU/software 回退**：`SwDecode`；当前绑定状态 `exact_definition`。
- **代码语义**：软件主图解码入口已绑定；真正的解码核心位于 codec 实现调用中。

**CPU 代码（精确函数定义）**：[openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp:565](../openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp:565)。

```cpp
   565  bool HeifDecoderImpl::SwDecode(bool isSharedMemory, uint32_t index, bool isAnimationDecode)
   566  {
   567      HevcSoftDecodeParam param {
   568              gridInfo_, Media::PixelFormat::UNKNOWN, outPixelFormat_,
   569              dstMemory_, 0,
   570              static_cast<uint32_t>(dstRowStride_), dstHwBuffer_,
   571              isSharedMemory, nullptr, static_cast<uint32_t>(dstRowStride_)
   572      };
   573      if (isAnimationDecode) {
   574          CHECK_ERROR_RETURN_RET_LOG(!animationImage_, false, "image sequence info is nullptr.");
   575          param.index = index;
   576          CHECK_ERROR_RETURN_RET(!SwDecodeImage(animationImage_, param, gridInfo_, false), false);
   577      } else {
   578          bool isNoPrimaryImageHeifs = animationImage_ && animationImage_->IsPrimaryImage();
   579          param.index = 0;
   580          CHECK_ERROR_RETURN_RET(!SwDecodeImage(primaryImage_, param, gridInfo_, !isNoPrimaryImageHeifs), false);
   581          SwApplyAlphaImage(primaryImage_, dstMemory_, dstRowStride_);
   582      }
   583      if (dstHwBuffer_ && (dstHwBuffer_->GetUsage() & BUFFER_USAGE_MEM_MMZ_CACHE)) {
   584          GSError err = dstHwBuffer_->Map();
   585          if (err != GSERROR_OK) {
   586              IMAGE_LOGE("SurfaceBuffer Map failed, GSError=%{public}d", err);
   587              return true;
   588          }
   589          err = dstHwBuffer_->FlushCache();
   590          if (err != GSERROR_OK) {
   591              IMAGE_LOGE("FlushCache failed, GSError=%{public}d", err);
   592          }
   593      }
   594      return true;
   595  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `HeifDecoderImpl::SwDecode`；`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp:565` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；31 行，31 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | bool isSharedMemory，uint32_t index，bool isAnimationDecode；局部类型：GSError, HevcSoftDecodeParam, bool | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=0，赋值=3，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `CHECK_ERROR_RETURN_RET` x2 (L576,580)；`CHECK_ERROR_RETURN_RET_LOG` x1 (L574)；`IMAGE_LOGE` x2 (L586,591)；`SwApplyAlphaImage` x1 (L581)；`SwDecodeImage` x2 (L576,580)；`animationImage_->IsPrimaryImage` x1 (L578)；`dstHwBuffer_->FlushCache` x1 (L589)；`dstHwBuffer_->GetUsage` x1 (L583)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`apply`, `decode` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `a2da175c5deec75a47d1565630753e71b8fe294c70084c34dfd162ec9ebcbae8`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：`HEIF hardware decoder` 与 CPU/software continuation 的回退关系已被源码确认；具体设备能力、格式约束与实际收益必须由运行时和 trace 证据门控。

### 5.11 GPU：image_texture_compression

**严格 pair**：`image_compress_gpu_to_raster_fallback`。

- **异构单元**：GPU，实现路径为 `ImageCompressor::GpuCompress`。
- **CPU/software 回退**：`RSImage::BuildFromBitmap`；当前绑定状态 `external_or_unresolved_definition`。
- **代码语义**：回退进入外部 raster 实现；仓内没有可唯一绑定的 CPU 压缩函数体。

**CPU 回退控制流证据（非唯一算法函数）**：

```cpp
   367              image->SetCompressData(compressData, width, height);
   368              image->ReplaceRSImage(nullptr);
   369          } else {
   370              auto rasterizedImage = std::make_shared<RSImage>();
   371              rasterizedImage->BuildFromBitmap(rsBitmap);
   372              image->ReplaceRSImage(rasterizedImage);
   373          }
   374          auto taskExecutor = Container::CurrentTaskExecutor();
   375          auto releaseTask = ImageCompressor::GetInstance()->ScheduleReleaseTask();
   376          if (taskExecutor) {
   377              taskExecutor->PostDelayedTask(releaseTask, TaskExecutor::TaskType::UI, ImageCompressor::releaseTimeMs,
   378                  "ArkUIImageCompressorScheduleRelease");
   379          } else {
   380              ImageUtils::PostToBg(std::move(releaseTask), "ArkUIImageCompressorScheduleRelease");
```

**AST / 绑定结论**：

| AST / 绑定项 | 结果 | 工程含义 |
| --- | --- | --- |
| 绑定状态 | `external_or_unresolved_definition` | 目标是外部库、虚函数或当前源码范围内无法唯一解析的定义。 |
| 请求符号 | `RSImage::BuildFromBitmap` | 这些符号描述路径，不等于可安全分析的算法函数 |
| 控制流证据 | [openharmony/foundation/arkui/ace_engine/frameworks/core/components_ng/image_provider/image_decoder.cpp:371](../openharmony/foundation/arkui/ace_engine/frameworks/core/components_ng/image_provider/image_decoder.cpp:371) | 展示真实回退边界 |
| 候选定义 | 无唯一函数定义 | 候选、factory 或 dispatcher 不可替代实际 CPU 算法 AST |
| AST 结论 | 未生成函数级算法 AST | 必须结合 trace、runtime descriptor、provider/codec 解析或模型实例继续下钻 |

- **异构映射与边界**：`GPU` 与 CPU fallback 的路径由源码确认。是否值得卸载仍取决于数据规模、格式支持、CPU-GPU 传输/同步开销和真实 trace 热点。

### 5.12 JPEG 硬件解码器：jpeg_decode

**严格 pair**：`jpeg_hardware_decode_to_skia_fallback`。

- **异构单元**：JPEG 硬件解码器，实现路径为 `ExtDecoder::DoHardWareDecode`。
- **CPU/software 回退**：`SkCodec::getPixels`；当前绑定状态 `external_or_unresolved_definition`。
- **代码语义**：回退调用 Skia 外部 codec API；调用点证明控制流，不证明 codec 算法函数体。

**CPU 回退控制流证据（非唯一算法函数）**：

```cpp
  1863          ImageUtils::FlushContextSurfaceBuffer(context);
  1864          return res;
  1865      }
  1866      CHECK_ERROR_RETURN_RET_LOG(codec_ == nullptr, ERR_IMAGE_DECODE_FAILED, "codec_ is nullptr");
  1867      SkCodec::Result ret = codec_->getPixels(dstInfo_, dstBuffer, rowStride, &dstOptions_);
  1868      if (ret == SkCodec::kIncompleteInput || ret == SkCodec::kErrorInInput) {
  1869          IMAGE_LOGI("Decode broken data success. Triggered kIncompleteInput feature of skia!");
  1870      } else if (ret != SkCodec::kSuccess && ResetCodec()) {
  1871          ret = codec_->getPixels(dstInfo_, dstBuffer, rowStride, &dstOptions_); // Try again
  1872      }
  1873      if (ret != SkCodec::kSuccess && ret != SkCodec::kIncompleteInput && ret != SkCodec::kErrorInInput) {
  1874          IMAGE_LOGE("Decode failed, get pixels failed, ret=%{public}d", ret);
  1875          SetHeifDecodeError(context);
  1876          ResetCodec(); // release old jpeg codec
```

**AST / 绑定结论**：

| AST / 绑定项 | 结果 | 工程含义 |
| --- | --- | --- |
| 绑定状态 | `external_or_unresolved_definition` | 目标是外部库、虚函数或当前源码范围内无法唯一解析的定义。 |
| 请求符号 | `SkCodec::getPixels` | 这些符号描述路径，不等于可安全分析的算法函数 |
| 控制流证据 | [openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_decoder.cpp:1867](../openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_decoder.cpp:1867) | 展示真实回退边界 |
| 候选定义 | 无唯一函数定义 | 候选、factory 或 dispatcher 不可替代实际 CPU 算法 AST |
| AST 结论 | 未生成函数级算法 AST | 必须结合 trace、runtime descriptor、provider/codec 解析或模型实例继续下钻 |

- **异构映射与边界**：`JPEG hardware decoder` 与 CPU/software continuation 的回退关系已被源码确认；具体设备能力、格式约束与实际收益必须由运行时和 trace 证据门控。

### 5.13 JPEG 硬件编码器：jpeg_encode

**严格 pair**：`jpeg_hardware_encode_to_skia_fallback`。

- **异构单元**：JPEG 硬件编码器，实现路径为 `ExtEncoder::HardwareEncode`。
- **CPU/software 回退**：`ExtEncoder::EncodeImageByBitmap via DoEncode`；当前绑定状态 `exact_definition`。
- **代码语义**：已绑定 CPU 函数将编码委托给软件后端，是 continuation/dispatch 边界，不是熵编码本体。

**CPU 代码（精确函数定义）**：[openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_encoder.cpp:721](../openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_encoder.cpp:721)。

```cpp
   721  uint32_t ExtEncoder::EncodeImageByBitmap(SkBitmap& bitmap, bool needExif, SkWStream& outStream)
   722  {
   723      ImageInfo imageInfo;
   724      pixelmap_->GetImageInfo(imageInfo);
   725      if (!needExif || pixelmap_->GetExifMetadata() == nullptr ||
   726          pixelmap_->GetExifMetadata()->GetExifData() == nullptr) {
   727              return DoEncode(&outStream, bitmap, encodeFormat_);
   728      }
   729  
   730      MetadataWStream tStream;
   731      uint32_t errCode = DoEncode(&tStream, bitmap, encodeFormat_);
   732      bool cond = errCode != SUCCESS;
   733      CHECK_ERROR_RETURN_RET(cond, errCode);
   734      return CreateAndWriteBlob(tStream, pixelmap_, outStream, imageInfo, opts_);
   735  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ExtEncoder::EncodeImageByBitmap`；`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_encoder.cpp:721` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；15 行，22 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | SkBitmap& bitmap，bool needExif，SkWStream& outStream；局部类型：ImageInfo, MetadataWStream, bool, uint32_t | 数据类型与接口边界 |
| 控制流 | 循环：无；if=1，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=2，赋值=0，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `CHECK_ERROR_RETURN_RET` x1 (L733)；`CreateAndWriteBlob` x1 (L734)；`DoEncode` x2 (L727,731)；`pixelmap_->GetExifMetadata` x2 (L725,726)；`pixelmap_->GetExifMetadata()->GetExifData` x1 (L726)；`pixelmap_->GetImageInfo` x1 (L724) | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access`, `encode` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `e57248decf815568ee5aed2bd78aad4e473d524cc4b00d2fac7b2d5a179d2e55`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：`JPEG hardware encoder` 与 CPU/software continuation 的回退关系已被源码确认；具体设备能力、格式约束与实际收益必须由运行时和 trace 证据门控。

### 5.14 OpenGL GPU：image_rotate

**严格 pair**：`pixelmap_gpu_rotate_to_cpu_fallback`。

- **异构单元**：OpenGL GPU，实现路径为 `PixelMapPostProcWithGL`。
- **CPU/software 回退**：`PixelMap::rotate`；当前绑定状态 `exact_definition`。
- **代码语义**：已绑定 PixelMap 方法是很短的 dispatcher；旋转算法需解析其 callee 后才能匹配。

**CPU 代码（精确函数定义）**：[openharmony/foundation/multimedia/image_framework/frameworks/innerkitsimpl/common/src/pixel_map.cpp:4864](../openharmony/foundation/multimedia/image_framework/frameworks/innerkitsimpl/common/src/pixel_map.cpp:4864)。

```cpp
  4864  void PixelMap::rotate(float degrees)
  4865  {
  4866      Rotate(degrees);
  4867  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `PixelMap::rotate`；`openharmony/foundation/multimedia/image_framework/frameworks/innerkitsimpl/common/src/pixel_map.cpp:4864` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；4 行，1 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | float degrees；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=0，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=0，赋值=0，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `Rotate` x1 (L4866) | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`rotate` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5e43d2648f87d07b118ed7c25fc7c15acb4b17e5376bc9b8157fcaad81a1c022`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：`OpenGL GPU` 与 CPU fallback 的路径由源码确认。是否值得卸载仍取决于数据规模、格式支持、CPU-GPU 传输/同步开销和真实 trace 热点。

### 5.15 OpenGL GPU：image_scale

**严格 pair**：`pixelmap_gpu_scale_to_cpu_fallback`。

- **异构单元**：OpenGL GPU，实现路径为 `PixelMapPostProcWithGL`。
- **CPU/software 回退**：`PostProc::ScalePixelMapEx`；当前绑定状态 `exact_definition`。
- **代码语义**：CPU 缩放 continuation 包含格式检查并调用软件 scaler；外部 scaler 才拥有 resize 循环。

**CPU 代码（精确函数定义）**：[openharmony/foundation/multimedia/image_framework/frameworks/innerkitsimpl/converter/src/post_proc.cpp:1176](../openharmony/foundation/multimedia/image_framework/frameworks/innerkitsimpl/converter/src/post_proc.cpp:1176)。

```cpp
  1176  bool PostProc::ScalePixelMapEx(const Size &desiredSize, PixelMap &pixelMap, const AntiAliasingOption &option)
  1177  {
  1178      ImageTrace imageTrace("PixelMap ScalePixelMapEx, srcSize[%d, %d], dstSize[%d, %d] ",
  1179          pixelMap.GetWidth(), pixelMap.GetHeight(), desiredSize.width, desiredSize.height);
  1180      IMAGE_LOGI("slr_gpu ScalePixelMapEx pixelMap: width = %{public}d, height = %{public}d, pixelFormat = %{public}d, "
  1181          "allocatorType = %{public}d; desiredSize: width = %{public}d, height = %{public}d",
  1182          pixelMap.GetWidth(), pixelMap.GetHeight(), pixelMap.GetPixelFormat(),
  1183          pixelMap.GetAllocatorType(), desiredSize.width, desiredSize.height);
  1184      ImageInfo imgInfo;
  1185      pixelMap.GetImageInfo(imgInfo);
  1186      int32_t srcWidth = pixelMap.GetWidth();
  1187      int32_t srcHeight = pixelMap.GetHeight();
  1188      bool cond = srcWidth <= 0 || srcHeight <= 0 || !pixelMap.GetWritablePixels();
  1189      CHECK_ERROR_RETURN_RET_LOG(cond, false, "pixelMap param is invalid, src width:%{public}d, height:%{public}d",
  1190          srcWidth, srcHeight);
  1191      AVPixelFormat pixelFormat;
  1192      cond = !GetScaleFormat(imgInfo.pixelFormat, pixelFormat);
  1193      CHECK_ERROR_RETURN_RET_LOG(cond, false, "pixelMap format is invalid, format: %{public}d", imgInfo.pixelFormat);
  1194      uint64_t dstBufferSizeOverflow =
  1195          static_cast<uint64_t>(desiredSize.width) * static_cast<uint64_t>(desiredSize.height) *
  1196          static_cast<uint64_t>(ImageUtils::GetPixelBytes(imgInfo.pixelFormat));
  1197      CHECK_ERROR_RETURN_RET_LOG(dstBufferSizeOverflow > UINT_MAX, false, "ScalePixelMapEx target size too large");
  1198      uint32_t dstBufferSize = static_cast<uint32_t>(dstBufferSizeOverflow);
  1199      MemoryData memoryData = {nullptr, dstBufferSize, "ScalePixelMapEx ImageData", desiredSize};
  1200      memoryData.usage = pixelMap.GetNoPaddingUsage();
  1201      auto mem = MemoryManager::CreateMemory(pixelMap.GetAllocatorType() == AllocatorType::CUSTOM_ALLOC ?
  1202          AllocatorType::DEFAULT : pixelMap.GetAllocatorType(), memoryData);
  1203      CHECK_ERROR_RETURN_RET_LOG(mem == nullptr, false, "ScalePixelMapEx CreateMemory failed");
  1204  
  1205      const uint8_t *srcPixels[FFMPEG_NUM] = {};
  1206      uint8_t *dstPixels[FFMPEG_NUM] = {};
  1207      srcPixels[0] = pixelMap.GetPixels();
  1208      dstPixels[0] = reinterpret_cast<uint8_t *>(mem->data.data);
  1209      int srcRowStride[FFMPEG_NUM] = {};
  1210      int dstRowStride[FFMPEG_NUM] = {};
  1211      srcRowStride[0] = pixelMap.GetRowStride();
  1212      dstRowStride[0] = (mem->GetType() == AllocatorType::DMA_ALLOC) ?
  1213          reinterpret_cast<SurfaceBuffer*>(mem->extend.data)->GetStride() :
  1214          desiredSize.width * ImageUtils::GetPixelBytes(imgInfo.pixelFormat);
  1215  
  1216      void *inBuf = nullptr;
  1217      if (srcWidth % HALF != 0 &&
  1218          (pixelMap.GetAllocatorType() == AllocatorType::SHARE_MEM_ALLOC ||
  1219          pixelMap.GetAllocatorType() == AllocatorType::HEAP_ALLOC || pixelMap.GetNoPaddingUsage())) {
  1220          // Workaround for crash on odd number width, caused by FFmpeg 5.0 upgrade
  1221          uint64_t byteCount = static_cast<uint64_t>(srcRowStride[0]) * static_cast<uint64_t>(srcHeight);
  1222          uint64_t allocSize = static_cast<uint64_t>(srcWidth + 1) * static_cast<uint64_t>(srcHeight) *
  1223              static_cast<uint64_t>(ImageUtils::GetPixelBytes(imgInfo.pixelFormat));
  1224          if (srcRowStride[0] <= 0 || byteCount > UINT_MAX || allocSize < byteCount || allocSize > UINT_MAX) {
  1225              mem->Release();
  1226              IMAGE_LOGE("ScalePixelMapEx invalid srcRowStride or pixelMap size too large");
  1227              return false;
  1228          }
  1229          inBuf = malloc(allocSize);
  1230          srcPixels[0] = reinterpret_cast<uint8_t*>(inBuf);
  1231          errno_t errRet = memcpy_s(inBuf, allocSize, pixelMap.GetWritablePixels(), byteCount);
  1232          if (errRet != EOK) {
  1233              if (inBuf != nullptr) {
  1234                  free(inBuf);
  1235              }
  1236              mem->Release();
  1237              IMAGE_LOGE("ScalePixelMapEx memcpy_s failed with error code: %{public}d", errRet);
  1238              return false;
  1239          }
  1240      }
  1241  
  1242      SwsContext *swsContext = sws_getContext(srcWidth, srcHeight, pixelFormat, desiredSize.width, desiredSize.height,
  1243          pixelFormat, GetInterpolation(option), nullptr, nullptr, nullptr);
  1244      if (swsContext == nullptr) {
  1245          if (inBuf != nullptr) {
  1246              free(inBuf);
  1247          }
  1248          mem->Release();
  1249          IMAGE_LOGE("sws_getContext failed");
  1250          return false;
  1251      }
  1252      auto res = sws_scale(swsContext, srcPixels, srcRowStride, 0, srcHeight, dstPixels, dstRowStride);
  1253  
  1254      sws_freeContext(swsContext);
  1255      if (inBuf != nullptr) {
  1256          free(inBuf);
  1257      }
  1258      if (!res) {
  1259          mem->Release();
  1260          IMAGE_LOGE("sws_scale failed");
  1261          return false;
  1262      }
  1263      pixelMap.SetPixelsAddr(mem->data.data, mem->extend.data, dstBufferSize, mem->GetType(), nullptr);
  1264      imgInfo.size = desiredSize;
  1265      pixelMap.SetImageInfo(imgInfo, true);
  1266      ImageUtils::FlushSurfaceBuffer(&pixelMap);
  1267      return true;
  1268  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `PostProc::ScalePixelMapEx`；`openharmony/foundation/multimedia/image_framework/frameworks/innerkitsimpl/converter/src/post_proc.cpp:1176` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；93 行，150 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | const Size &desiredSize，PixelMap &pixelMap，const AntiAliasingOption &option；局部类型：AVPixelFormat, ImageInfo, ImageTrace, MemoryData, SwsContext, bool, errno_t, int, int32_t, uint32_t, uint64_t, uint8_t, void | 数据类型与接口边界 |
| 控制流 | 循环：无；if=8，switch=0，ternary=2 | 区分调度循环与数据并行循环 |
| 访存 | 下标=7，指针=1，赋值=9，内存库调用=5 | 只描述本函数体可见的访存 |
| 调用 | `CHECK_ERROR_RETURN_RET_LOG` x2 (L1197,1203)；`CHECK_ERROR_RETURN_RET_LOG` x1 (L1193)；`CHECK_ERROR_RETURN_RET_LOG` x1 (L1189)；`GetInterpolation` x1 (L1243)；`GetScaleFormat` x1 (L1192)；`IMAGE_LOGE` x3 (L1226,1249,1260)；`IMAGE_LOGE` x1 (L1237)；`IMAGE_LOGI` x1 (L1180)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access`, `memory_copy_or_fill`, `scale` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `a75170f8753873721c1073fd46d4c3303f2d16a433f21e274c13a3fb6abec949`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：`OpenGL GPU` 与 CPU fallback 的路径由源码确认。是否值得卸载仍取决于数据规模、格式支持、CPU-GPU 传输/同步开销和真实 trace 热点。

### 5.16 硬件视频解码器：video_thumbnail_decode

**严格 pair**：`thumbnail_hardware_decoder_to_software_reinitialization`。

- **异构单元**：硬件视频解码器，实现路径为 `MediaAVCodec::VideoDecoderFactory::CreateByMime`。
- **CPU/software 回退**：`AVThumbnailGenerator::InitDecoder with AVCODEC_SOFTWARE`；当前绑定状态 `runtime_selected_backend`。
- **代码语义**：回退切换到软件 decoder category 并重新初始化 runtime codec；未选择固定解码算法函数。

**CPU 回退控制流证据（非唯一算法函数）**：

```cpp
   294          }
   295          auto res = SeekToTime(Plugins::Us2Ms(currentFetchFrameYuvTimeUs_),
   296              static_cast<Plugins::SeekMode>(currentFetchFrameYuvOption_), currentFetchFrameYuvTimeUs_);
   297          CHECK_AND_RETURN_LOG(res == Status::OK, "Seek failed.");
   298          CHECK_AND_RETURN_LOG(InitDecoder(codecName) == Status::OK, "Failed to create software decoder.");
   299      }
   300      MEDIA_LOGI("SwitchToSoftWareDecoder end");
   301  }
   302  
   303  int32_t AVThumbnailGenerator::Init()
   304  {
   305      CHECK_AND_RETURN_RET_LOG(inputBufferQueue_ == nullptr, MSERR_OK, "InputBufferQueue already create");
   306  
   307      inputBufferQueue_ = AVBufferQueue::Create(0,
```

**AST / 绑定结论**：

| AST / 绑定项 | 结果 | 工程含义 |
| --- | --- | --- |
| 绑定状态 | `runtime_selected_backend` | 软件 backend 在运行时选择；factory/capability 代码不是编解码算法函数。 |
| 请求符号 | `AVThumbnailGenerator::InitDecoder`, `InitDecoder` | 这些符号描述路径，不等于可安全分析的算法函数 |
| 控制流证据 | [openharmony/foundation/multimedia/player_framework/services/engine/histreamer/avmetadatahelper/av_thumbnail_generator.cpp:298](../openharmony/foundation/multimedia/player_framework/services/engine/histreamer/avmetadatahelper/av_thumbnail_generator.cpp:298) | 展示真实回退边界 |
| 候选定义 | 无唯一函数定义 | 候选、factory 或 dispatcher 不可替代实际 CPU 算法 AST |
| AST 结论 | 未生成函数级算法 AST | 必须结合 trace、runtime descriptor、provider/codec 解析或模型实例继续下钻 |

- **异构映射与边界**：`hardware video decoder` 与 CPU/software continuation 的回退关系已被源码确认；具体设备能力、格式约束与实际收益必须由运行时和 trace 证据门控。

### 5.17 硬件视频解码器：video_decode

**严格 pair**：`video_decoder_surface_hardware_to_software_capability_fallback`。

- **异构单元**：硬件视频解码器，实现路径为 `DecoderSurfaceFilter::CheckDecoderCapability(true)`。
- **CPU/software 回退**：`DecoderSurfaceFilter::CheckDecoderCapability(false) and VideoDecoderAdapter::Init`；当前绑定状态 `runtime_selected_backend`。
- **代码语义**：回退在运行时选择 software capability 或 software decoder；实现不是唯一源码函数。

**CPU 回退控制流证据（非唯一算法函数）**：

```cpp
   406          useHwDecoder = true;
   407          return Status::OK;
   408      }
   409  
   410      if (CheckDecoderCapability(false, videoWidth, videoHeight, decoderName)) {
   411          MEDIA_LOG_I("Video size %{public}dx%{public}d supported by sw decoder %{public}s",
   412              videoWidth, videoHeight, decoderName.c_str());
   413          useHwDecoder = false;
   414          return Status::OK;
   415      }
   416  
   417      MEDIA_LOG_I("Video size %{public}dx%{public}d not supported by both hw and sw decoder", videoWidth, videoHeight);
   418      return Status::ERROR_UNSUPPORTED_FORMAT;
   419  }
```

**AST / 绑定结论**：

| AST / 绑定项 | 结果 | 工程含义 |
| --- | --- | --- |
| 绑定状态 | `runtime_selected_backend` | 软件 backend 在运行时选择；factory/capability 代码不是编解码算法函数。 |
| 请求符号 | `DecoderSurfaceFilter::CheckDecoderCapability`, `VideoDecoderAdapter::Init`, `CheckDecoderCapability` | 这些符号描述路径，不等于可安全分析的算法函数 |
| 控制流证据 | [openharmony/foundation/multimedia/av_codec/services/media_engine/filters/decoder_surface_filter.cpp:410](../openharmony/foundation/multimedia/av_codec/services/media_engine/filters/decoder_surface_filter.cpp:410) | 展示真实回退边界 |
| 候选定义 | 无唯一函数定义 | 候选、factory 或 dispatcher 不可替代实际 CPU 算法 AST |
| AST 结论 | 未生成函数级算法 AST | 必须结合 trace、runtime descriptor、provider/codec 解析或模型实例继续下钻 |

- **异构映射与边界**：`hardware video decoder` 与 CPU/software continuation 的回退关系已被源码确认；具体设备能力、格式约束与实际收益必须由运行时和 trace 证据门控。

### 5.18 硬件视频解码器：video_decode

**严格 pair**：`webview_video_decoder_hardware_to_software_creation_fallback`。

- **异构单元**：硬件视频解码器，实现路径为 `OH_AVCodec_GetCapabilityByCategory(..., HARDWARE) plus OH_VideoDecoder_CreateByName`。
- **CPU/software 回退**：`OH_AVCodec_GetCapabilityByCategory(..., SOFTWARE) plus OH_VideoDecoder_CreateByName`；当前绑定状态 `runtime_selected_backend`。
- **代码语义**：回退在运行时选择 software capability 或 software decoder；实现不是唯一源码函数。

**CPU 回退控制流证据（非唯一算法函数）**：

```cpp
   102              return DecoderAdapterCode::DECODER_OK;
   103          }
   104      }
   105  
   106      avCap_ = OH_AVCodec_GetCapabilityByCategory(name.c_str(), false, SOFTWARE);
   107      if (avCap_ != nullptr) {
   108          decoderName = std::string(OH_AVCapability_GetName(avCap_));
   109          if (isSecure_) {
   110              decoderName += ".secure";
   111          }
   112          decoder_ = OH_VideoDecoder_CreateByName(decoderName.c_str());
   113          if (decoder_ != nullptr) {
   114              isHardwareDecode_ = false;
   115              WVLOG_I("MediaCodecDecoder create software decoder.");
```

**AST / 绑定结论**：

| AST / 绑定项 | 结果 | 工程含义 |
| --- | --- | --- |
| 绑定状态 | `runtime_selected_backend` | 软件 backend 在运行时选择；factory/capability 代码不是编解码算法函数。 |
| 请求符号 | `OH_AVCodec_GetCapabilityByCategory` | 这些符号描述路径，不等于可安全分析的算法函数 |
| 控制流证据 | [openharmony/base/web/webview/ohos_adapter/media_adapter/src/media_codec_decoder_adapter_impl.cpp:106](../openharmony/base/web/webview/ohos_adapter/media_adapter/src/media_codec_decoder_adapter_impl.cpp:106) | 展示真实回退边界 |
| 候选定义 | 无唯一函数定义 | 候选、factory 或 dispatcher 不可替代实际 CPU 算法 AST |
| AST 结论 | 未生成函数级算法 AST | 必须结合 trace、runtime descriptor、provider/codec 解析或模型实例继续下钻 |

- **异构映射与边界**：`hardware video decoder` 与 CPU/software continuation 的回退关系已被源码确认；具体设备能力、格式约束与实际收益必须由运行时和 trace 证据门控。

### 5.19 Kirin NPU（HiAI DDK）：mindspore_add_fusion

**严格 pair**：`mindspore_kirin_npu_add_fusion_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::ArithmeticNPUOp for PrimitiveType_AddFusion`。
- **CPU/software 回退**：`ArithmeticCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：逐元素张量算术由同一 primitive 的 NPU delegate 与 CPU arithmetic kernel 注册关联。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49)。

```cpp
    49  int ArithmeticCPUKernel::Run() {
    50    if (in_tensors_[FIRST_INPUT]->data_type() != in_tensors_[SECOND_INPUT]->data_type()) {
    51      MS_LOG(ERROR) << "The dataTypes of input0 and input1 should be the same. input0: "
    52                    << in_tensors_[FIRST_INPUT]->data_type() << " vs input1" << in_tensors_[SECOND_INPUT]->data_type();
    53      return RET_ERROR;
    54    }
    55    if (op_parameter_->is_train_session_) {
    56      in_data_size_ = static_cast<int>(lite::DataTypeSize(in_tensors_.front()->data_type()));
    57    }
    58    if (!a_matric_.is_valid) {
    59      a_matric_.data = in_tensors_[FIRST_INPUT]->data();
    60    }
    61    if (!b_matric_.is_valid) {
    62      b_matric_.data = in_tensors_[SECOND_INPUT]->data();
    63    }
    64    c_matric_.data = out_tensors_.front()->data();
    65    return ArithmeticBaseCPUKernel::Run();
    66  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ArithmeticCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；18 行，35 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=6，指针=0，赋值=4，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ArithmeticBaseCPUKernel::Run` x1 (L65)；`MS_LOG` x1 (L51)；`in_tensors_.front` x1 (L56)；`in_tensors_.front()->data_type` x1 (L56)；`in_tensors_[FIRST_INPUT]->data` x1 (L59)；`in_tensors_[FIRST_INPUT]->data_type` x2 (L50,52)；`in_tensors_[SECOND_INPUT]->data` x1 (L62)；`in_tensors_[SECOND_INPUT]->data_type` x2 (L50,52)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f62958496ab6cdbe931737725a407adf7566399e5e33779455b665653282803`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_add_fusion` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.20 Kirin NPU（HiAI DDK）：mindspore_broadcast_to

**严格 pair**：`mindspore_kirin_npu_broadcast_to_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::BroadcastToNPUOp for PrimitiveType_BroadcastTo`。
- **CPU/software 回退**：`BroadcastToCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：Broadcast 是 shape-aware 张量扩展；Run 入口把元素遍历委托给 kernel helper。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/broadcast_to_fp32.cc:52](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/broadcast_to_fp32.cc:52)。

```cpp
    52  int BroadcastToCPUKernel::Run() {
    53    const auto input_data = in_tensors_.at(0)->data();
    54    auto output_data = out_tensors_.at(0)->data();
    55    CHECK_NULL_RETURN(input_data);
    56    CHECK_NULL_RETURN(output_data);
    57  
    58    auto data_type = in_tensors_.at(0)->data_type();
    59    switch (data_type) {
    60      case kNumberTypeFloat32:
    61        return BroadcastToSize32(input_data, &shape_info_, output_data);
    62      case kNumberTypeFloat16:
    63        return BroadcastToSize16(input_data, &shape_info_, output_data);
    64      case kNumberTypeInt32:
    65      case kNumberTypeInt:
    66        return BroadcastToSize32(input_data, &shape_info_, output_data);
    67      case kNumberTypeBool:
    68        return BroadcastToSize8(input_data, &shape_info_, output_data);
    69      case kNumberTypeUInt8:
    70        return BroadcastToSize8(input_data, &shape_info_, output_data);
    71      default:
    72        MS_LOG(ERROR) << "UnSupported data type: " << data_type;
    73        return RET_ERROR;
    74    }
    75  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `BroadcastToCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/broadcast_to_fp32.cc:52` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；24 行，31 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=0，switch=1，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=5，赋值=0，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `BroadcastToSize16` x1 (L63)；`BroadcastToSize32` x2 (L61,66)；`BroadcastToSize8` x2 (L68,70)；`CHECK_NULL_RETURN` x2 (L55,56)；`MS_LOG` x1 (L72)；`in_tensors_.at` x2 (L53,58)；`in_tensors_.at(0)->data` x1 (L53)；`in_tensors_.at(0)->data_type` x1 (L58)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `365ed5ef2b6d4e8fb05c9377a0a57b7a57df3434004ea5db262f1920e9d11f4d`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_broadcast_to` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.21 Kirin NPU（HiAI DDK）：mindspore_cast

**严格 pair**：`mindspore_kirin_npu_cast_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::CastNPUOp for PrimitiveType_Cast`。
- **CPU/software 回退**：`CastCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：Cast 是 dtype 转换 primitive；短 Run 入口委托给按 tensor type 选择的转换 kernel。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/cast_fp32.cc:211](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/cast_fp32.cc:211)。

```cpp
   211  int CastCPUKernel::Run() {
   212    if (data_num_ == 0) {
   213      return RET_OK;
   214    }
   215    return ParallelLaunch(this->ms_context_, CastRun, this, op_parameter_->thread_num_);
   216  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `CastCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/cast_fp32.cc:211` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；6 行，5 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=1，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=0，赋值=0，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ParallelLaunch` x1 (L215) | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；无 | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `7878b8c54d4854e34a331549731942e811c6742e4f3e7149f59309c600ba823e`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_cast` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.22 Kirin NPU（HiAI DDK）：mindspore_conv2d_fusion

**严格 pair**：`mindspore_kirin_npu_conv2d_fusion_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::GetNPUConvOp for PrimitiveType_Conv2DFusion`。
- **CPU/software 回退**：`CpuConvFp32KernelCreator`；当前绑定状态 `runtime_selected_cpu_kernel`。
- **代码语义**：CPU 侧是 kernel factory；shape、dtype 与 packing 在运行时选择真实 convolution 函数体。

**CPU 回退控制流证据（非唯一算法函数）**：

```cpp
   387    return group_kernel;
   388  }
   389  
   390  /* creator func */
   391  kernel::LiteKernel *CpuConvFp32KernelCreator(const std::vector<lite::Tensor *> &inputs,
   392                                               const std::vector<lite::Tensor *> &outputs, OpParameter *op_parameter,
   393                                               const InnerContext *ctx, const kernel::KernelKey &desc) {
   394    MS_ASSERT(op_parameter != nullptr);
   395    MS_ASSERT(desc.type == schema::PrimitiveType_Conv2DFusion);
   396    MS_ASSERT(desc.data_type == kNumberTypeFloat32);
   397  
   398    auto conv_param = reinterpret_cast<ConvParameter *>(op_parameter);
   399    kernel::LiteKernel *kernel = nullptr;
   400    if (conv_param->group_ == 1) {
```

**AST / 绑定结论**：

| AST / 绑定项 | 结果 | 工程含义 |
| --- | --- | --- |
| 绑定状态 | `runtime_selected_cpu_kernel` | CPU kernel factory 在运行时选择具体 kernel；factory 不能作为算法函数 AST。 |
| 请求符号 | `CpuConvFp32KernelCreator` | 这些符号描述路径，不等于可安全分析的算法函数 |
| 控制流证据 | [openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/convolution_delegate_fp32.cc:391](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/convolution_delegate_fp32.cc:391) | 展示真实回退边界 |
| 候选定义 | 无唯一函数定义 | 候选、factory 或 dispatcher 不可替代实际 CPU 算法 AST |
| AST 结论 | 未生成函数级算法 AST | 必须结合 trace、runtime descriptor、provider/codec 解析或模型实例继续下钻 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_conv2d_fusion` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.23 Kirin NPU（HiAI DDK）：mindspore_conv2d_transpose_fusion

**严格 pair**：`mindspore_kirin_npu_conv2d_transpose_fusion_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::DeconvolutionNPUOp for PrimitiveType_Conv2dTransposeFusion`。
- **CPU/software 回退**：`CpuDeConvFp32KernelCreator`；当前绑定状态 `runtime_selected_cpu_kernel`。
- **代码语义**：CPU 侧是 deconvolution kernel factory；实际计算体依赖运行时 tensor 配置。

**CPU 回退控制流证据（非唯一算法函数）**：

```cpp
   357  #endif
   358    return kernel;
   359  }
   360  
   361  kernel::LiteKernel *CpuDeConvFp32KernelCreator(const std::vector<lite::Tensor *> &inputs,
   362                                                 const std::vector<lite::Tensor *> &outputs, OpParameter *op_parameter,
   363                                                 const InnerContext *ctx, const kernel::KernelKey &desc) {
   364    MS_CHECK_TRUE_RET(op_parameter != nullptr, nullptr);
   365    MS_CHECK_TRUE_RET(ctx != nullptr, nullptr);
   366  
   367    MS_ASSERT(desc.type == schema::PrimitiveType_Conv2dTransposeFusion);
   368  
   369    auto conv_param = reinterpret_cast<ConvParameter *>(op_parameter);
   370    kernel::LiteKernel *kernel = nullptr;
```

**AST / 绑定结论**：

| AST / 绑定项 | 结果 | 工程含义 |
| --- | --- | --- |
| 绑定状态 | `runtime_selected_cpu_kernel` | CPU kernel factory 在运行时选择具体 kernel；factory 不能作为算法函数 AST。 |
| 请求符号 | `CpuDeConvFp32KernelCreator` | 这些符号描述路径，不等于可安全分析的算法函数 |
| 控制流证据 | [openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/deconvolution_fp32.cc:361](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/deconvolution_fp32.cc:361) | 展示真实回退边界 |
| 候选定义 | 无唯一函数定义 | 候选、factory 或 dispatcher 不可替代实际 CPU 算法 AST |
| AST 结论 | 未生成函数级算法 AST | 必须结合 trace、runtime descriptor、provider/codec 解析或模型实例继续下钻 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_conv2d_transpose_fusion` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.24 Kirin NPU（HiAI DDK）：mindspore_div_fusion

**严格 pair**：`mindspore_kirin_npu_div_fusion_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::ArithmeticNPUOp for PrimitiveType_DivFusion`。
- **CPU/software 回退**：`ArithmeticCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：逐元素除法复用 CPU arithmetic dispatcher；实际 operation 在参数化 callee 中选择。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49)。

```cpp
    49  int ArithmeticCPUKernel::Run() {
    50    if (in_tensors_[FIRST_INPUT]->data_type() != in_tensors_[SECOND_INPUT]->data_type()) {
    51      MS_LOG(ERROR) << "The dataTypes of input0 and input1 should be the same. input0: "
    52                    << in_tensors_[FIRST_INPUT]->data_type() << " vs input1" << in_tensors_[SECOND_INPUT]->data_type();
    53      return RET_ERROR;
    54    }
    55    if (op_parameter_->is_train_session_) {
    56      in_data_size_ = static_cast<int>(lite::DataTypeSize(in_tensors_.front()->data_type()));
    57    }
    58    if (!a_matric_.is_valid) {
    59      a_matric_.data = in_tensors_[FIRST_INPUT]->data();
    60    }
    61    if (!b_matric_.is_valid) {
    62      b_matric_.data = in_tensors_[SECOND_INPUT]->data();
    63    }
    64    c_matric_.data = out_tensors_.front()->data();
    65    return ArithmeticBaseCPUKernel::Run();
    66  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ArithmeticCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；18 行，35 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=6，指针=0，赋值=4，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ArithmeticBaseCPUKernel::Run` x1 (L65)；`MS_LOG` x1 (L51)；`in_tensors_.front` x1 (L56)；`in_tensors_.front()->data_type` x1 (L56)；`in_tensors_[FIRST_INPUT]->data` x1 (L59)；`in_tensors_[FIRST_INPUT]->data_type` x2 (L50,52)；`in_tensors_[SECOND_INPUT]->data` x1 (L62)；`in_tensors_[SECOND_INPUT]->data_type` x2 (L50,52)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f62958496ab6cdbe931737725a407adf7566399e5e33779455b665653282803`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_div_fusion` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.25 Kirin NPU（HiAI DDK）：mindspore_eltwise

**严格 pair**：`mindspore_kirin_npu_eltwise_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::EltwiseNPUOp for PrimitiveType_Eltwise`。
- **CPU/software 回退**：`ArithmeticCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：Eltwise 使用参数化 CPU arithmetic dispatcher；需解析 operation parameter 才能获得计算核心 AST。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49)。

```cpp
    49  int ArithmeticCPUKernel::Run() {
    50    if (in_tensors_[FIRST_INPUT]->data_type() != in_tensors_[SECOND_INPUT]->data_type()) {
    51      MS_LOG(ERROR) << "The dataTypes of input0 and input1 should be the same. input0: "
    52                    << in_tensors_[FIRST_INPUT]->data_type() << " vs input1" << in_tensors_[SECOND_INPUT]->data_type();
    53      return RET_ERROR;
    54    }
    55    if (op_parameter_->is_train_session_) {
    56      in_data_size_ = static_cast<int>(lite::DataTypeSize(in_tensors_.front()->data_type()));
    57    }
    58    if (!a_matric_.is_valid) {
    59      a_matric_.data = in_tensors_[FIRST_INPUT]->data();
    60    }
    61    if (!b_matric_.is_valid) {
    62      b_matric_.data = in_tensors_[SECOND_INPUT]->data();
    63    }
    64    c_matric_.data = out_tensors_.front()->data();
    65    return ArithmeticBaseCPUKernel::Run();
    66  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ArithmeticCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；18 行，35 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=6，指针=0，赋值=4，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ArithmeticBaseCPUKernel::Run` x1 (L65)；`MS_LOG` x1 (L51)；`in_tensors_.front` x1 (L56)；`in_tensors_.front()->data_type` x1 (L56)；`in_tensors_[FIRST_INPUT]->data` x1 (L59)；`in_tensors_[FIRST_INPUT]->data_type` x2 (L50,52)；`in_tensors_[SECOND_INPUT]->data` x1 (L62)；`in_tensors_[SECOND_INPUT]->data_type` x2 (L50,52)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f62958496ab6cdbe931737725a407adf7566399e5e33779455b665653282803`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_eltwise` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.26 Kirin NPU（HiAI DDK）：mindspore_floor_div

**严格 pair**：`mindspore_kirin_npu_floor_div_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::ArithmeticNPUOp for PrimitiveType_FloorDiv`。
- **CPU/software 回退**：`ArithmeticCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：FloorDiv 复用参数化 CPU arithmetic dispatcher；实际 inner operation 在 Run 之下。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49)。

```cpp
    49  int ArithmeticCPUKernel::Run() {
    50    if (in_tensors_[FIRST_INPUT]->data_type() != in_tensors_[SECOND_INPUT]->data_type()) {
    51      MS_LOG(ERROR) << "The dataTypes of input0 and input1 should be the same. input0: "
    52                    << in_tensors_[FIRST_INPUT]->data_type() << " vs input1" << in_tensors_[SECOND_INPUT]->data_type();
    53      return RET_ERROR;
    54    }
    55    if (op_parameter_->is_train_session_) {
    56      in_data_size_ = static_cast<int>(lite::DataTypeSize(in_tensors_.front()->data_type()));
    57    }
    58    if (!a_matric_.is_valid) {
    59      a_matric_.data = in_tensors_[FIRST_INPUT]->data();
    60    }
    61    if (!b_matric_.is_valid) {
    62      b_matric_.data = in_tensors_[SECOND_INPUT]->data();
    63    }
    64    c_matric_.data = out_tensors_.front()->data();
    65    return ArithmeticBaseCPUKernel::Run();
    66  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ArithmeticCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；18 行，35 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=6，指针=0，赋值=4，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ArithmeticBaseCPUKernel::Run` x1 (L65)；`MS_LOG` x1 (L51)；`in_tensors_.front` x1 (L56)；`in_tensors_.front()->data_type` x1 (L56)；`in_tensors_[FIRST_INPUT]->data` x1 (L59)；`in_tensors_[FIRST_INPUT]->data_type` x2 (L50,52)；`in_tensors_[SECOND_INPUT]->data` x1 (L62)；`in_tensors_[SECOND_INPUT]->data_type` x2 (L50,52)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f62958496ab6cdbe931737725a407adf7566399e5e33779455b665653282803`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_floor_div` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.27 Kirin NPU（HiAI DDK）：mindspore_floor_mod

**严格 pair**：`mindspore_kirin_npu_floor_mod_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::ArithmeticNPUOp for PrimitiveType_FloorMod`。
- **CPU/software 回退**：`ArithmeticCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：FloorMod 复用参数化 CPU arithmetic dispatcher；实际 inner operation 在 Run 之下。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49)。

```cpp
    49  int ArithmeticCPUKernel::Run() {
    50    if (in_tensors_[FIRST_INPUT]->data_type() != in_tensors_[SECOND_INPUT]->data_type()) {
    51      MS_LOG(ERROR) << "The dataTypes of input0 and input1 should be the same. input0: "
    52                    << in_tensors_[FIRST_INPUT]->data_type() << " vs input1" << in_tensors_[SECOND_INPUT]->data_type();
    53      return RET_ERROR;
    54    }
    55    if (op_parameter_->is_train_session_) {
    56      in_data_size_ = static_cast<int>(lite::DataTypeSize(in_tensors_.front()->data_type()));
    57    }
    58    if (!a_matric_.is_valid) {
    59      a_matric_.data = in_tensors_[FIRST_INPUT]->data();
    60    }
    61    if (!b_matric_.is_valid) {
    62      b_matric_.data = in_tensors_[SECOND_INPUT]->data();
    63    }
    64    c_matric_.data = out_tensors_.front()->data();
    65    return ArithmeticBaseCPUKernel::Run();
    66  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ArithmeticCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；18 行，35 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=6，指针=0，赋值=4，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ArithmeticBaseCPUKernel::Run` x1 (L65)；`MS_LOG` x1 (L51)；`in_tensors_.front` x1 (L56)；`in_tensors_.front()->data_type` x1 (L56)；`in_tensors_[FIRST_INPUT]->data` x1 (L59)；`in_tensors_[FIRST_INPUT]->data_type` x2 (L50,52)；`in_tensors_[SECOND_INPUT]->data` x1 (L62)；`in_tensors_[SECOND_INPUT]->data_type` x2 (L50,52)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f62958496ab6cdbe931737725a407adf7566399e5e33779455b665653282803`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_floor_mod` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.28 Kirin NPU（HiAI DDK）：mindspore_full_connection

**严格 pair**：`mindspore_kirin_npu_full_connection_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::FullconnectionNPUOp for PrimitiveType_FullConnection`。
- **CPU/software 回退**：`FullconnectionFP16CPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：FP16 fully-connected Run 将矩阵/向量计算委托给基类；weight packing 与 MAC 循环在入口之下。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp16/fullconnection_fp16.cc:74](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp16/fullconnection_fp16.cc:74)。

```cpp
    74  int FullconnectionFP16CPUKernel::Run() {
    75    auto ret = MatmulBaseFP16CPUKernel::Run();
    76    if (ret != RET_OK) {
    77      MS_LOG(ERROR) << "FullconnectionFP16CPUKernel run failed";
    78    }
    79    return ret;
    80  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `FullconnectionFP16CPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp16/fullconnection_fp16.cc:74` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；7 行，7 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=1，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=0，赋值=0，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `MS_LOG` x1 (L77)；`MatmulBaseFP16CPUKernel::Run` x1 (L75) | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；无 | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `6b7234384b0784ad6126a79784415e16082e2f6d6301dbc3998116cc6e4e96aa`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_full_connection` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.29 Kirin NPU（HiAI DDK）：mindspore_instance_norm

**严格 pair**：`mindspore_kirin_npu_instance_norm_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::InstanceNormNPUOp for PrimitiveType_InstanceNorm`。
- **CPU/software 回退**：`InstanceNormCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：InstanceNorm Run 暴露 tensor shape 检查和调度；reduction/normalization 循环需 callee 分析。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/instance_norm_fp32.cc:102](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/instance_norm_fp32.cc:102)。

```cpp
   102  int InstanceNormCPUKernel::Run() {
   103    src_data_ = reinterpret_cast<float *>(in_tensors_.at(FIRST_INPUT)->data());
   104    gamma_data_ = reinterpret_cast<float *>(in_tensors_.at(SECOND_INPUT)->data());
   105    beta_data_ = reinterpret_cast<float *>(in_tensors_.at(THIRD_INPUT)->data());
   106    dst_data_ = reinterpret_cast<float *>(out_tensors_.at(FIRST_INPUT)->data());
   107    CHECK_NULL_RETURN(src_data_);
   108    CHECK_NULL_RETURN(gamma_data_);
   109    CHECK_NULL_RETURN(beta_data_);
   110    CHECK_NULL_RETURN(dst_data_);
   111    if (in_tensors_[0]->format() == NC4HW4) {
   112  #if defined(ENABLE_AVX) || defined(ENABLE_ARM64)
   113      tmp_src_data_ = src_data_;
   114  #else  // other platform is not support nc4hw4 and must be pack to nc4hw4
   115      tmp_src_data_ = reinterpret_cast<float *>(ms_context_->allocator->Malloc(in_tensors_[0]->Size()));
   116      CHECK_NULL_RETURN(tmp_src_data_);
   117      PackNHWCToNC4HW4NotAlignedFp32(src_data_, tmp_src_data_, param_->batch_, param_->inner_size_, param_->channel_);
   118  #endif
   119      input_pack_to_nc4hw4_ = true;
   120    } else if (in_tensors_[0]->format() == NHWC) {
   121      tmp_src_data_ = reinterpret_cast<float *>(ms_context_->allocator->Malloc(in_tensors_[0]->Size()));
   122      CHECK_NULL_RETURN(tmp_src_data_);
   123  #ifdef ENABLE_AVX
   124      PackNHWCToNC8HW8NotAlignedFp32(src_data_, tmp_src_data_, param_->batch_, param_->inner_size_, param_->channel_);
   125  #else
   126      PackNHWCToNC4HW4NotAlignedFp32(src_data_, tmp_src_data_, param_->batch_, param_->inner_size_, param_->channel_);
   127  #endif
   128      input_pack_to_nc4hw4_ = true;
   129    } else {
   130      tmp_src_data_ = src_data_;
   131    }
   132    auto ret = ParallelLaunch(this->ms_context_, InstanceNormRun, this, op_parameter_->thread_num_);
   133    if (ret != RET_OK) {
   134      MS_LOG(ERROR) << "InstanceNormRun error error_code[" << ret << "]";
   135    }
   136    if (tmp_src_data_ != src_data_) {
   137      FreeTmpBuffer();
   138    }
   139    return ret;
   140  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `InstanceNormCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/instance_norm_fp32.cc:102` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；39 行，51 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=3，指针=0，赋值=9，内存库调用=1 | 只描述本函数体可见的访存 |
| 调用 | `CHECK_NULL_RETURN` x5 (L107,108,109,110,122)；`FreeTmpBuffer` x1 (L137)；`MS_LOG` x1 (L134)；`PackNHWCToNC8HW8NotAlignedFp32` x1 (L124)；`ParallelLaunch` x1 (L132)；`in_tensors_.at` x3 (L103,104,105)；`in_tensors_.at(FIRST_INPUT)->data` x1 (L103)；`in_tensors_.at(SECOND_INPUT)->data` x1 (L104)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access`, `memory_copy_or_fill` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `21b1753c6d8dd66a8c86e49dd1e5f1af13625234d71495f2873dc0af4a0902a3`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_instance_norm` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.30 Kirin NPU（HiAI DDK）：mindspore_logical_and

**严格 pair**：`mindspore_kirin_npu_logical_and_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::ArithmeticNPUOp for PrimitiveType_LogicalAnd`。
- **CPU/software 回退**：`ArithmeticCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：LogicalAnd 复用参数化 CPU arithmetic dispatcher；dtype 和 operation parameter 决定具体操作。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49)。

```cpp
    49  int ArithmeticCPUKernel::Run() {
    50    if (in_tensors_[FIRST_INPUT]->data_type() != in_tensors_[SECOND_INPUT]->data_type()) {
    51      MS_LOG(ERROR) << "The dataTypes of input0 and input1 should be the same. input0: "
    52                    << in_tensors_[FIRST_INPUT]->data_type() << " vs input1" << in_tensors_[SECOND_INPUT]->data_type();
    53      return RET_ERROR;
    54    }
    55    if (op_parameter_->is_train_session_) {
    56      in_data_size_ = static_cast<int>(lite::DataTypeSize(in_tensors_.front()->data_type()));
    57    }
    58    if (!a_matric_.is_valid) {
    59      a_matric_.data = in_tensors_[FIRST_INPUT]->data();
    60    }
    61    if (!b_matric_.is_valid) {
    62      b_matric_.data = in_tensors_[SECOND_INPUT]->data();
    63    }
    64    c_matric_.data = out_tensors_.front()->data();
    65    return ArithmeticBaseCPUKernel::Run();
    66  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ArithmeticCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；18 行，35 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=6，指针=0，赋值=4，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ArithmeticBaseCPUKernel::Run` x1 (L65)；`MS_LOG` x1 (L51)；`in_tensors_.front` x1 (L56)；`in_tensors_.front()->data_type` x1 (L56)；`in_tensors_[FIRST_INPUT]->data` x1 (L59)；`in_tensors_[FIRST_INPUT]->data_type` x2 (L50,52)；`in_tensors_[SECOND_INPUT]->data` x1 (L62)；`in_tensors_[SECOND_INPUT]->data_type` x2 (L50,52)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f62958496ab6cdbe931737725a407adf7566399e5e33779455b665653282803`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_logical_and` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.31 Kirin NPU（HiAI DDK）：mindspore_logical_or

**严格 pair**：`mindspore_kirin_npu_logical_or_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::ArithmeticNPUOp for PrimitiveType_LogicalOr`。
- **CPU/software 回退**：`ArithmeticCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：LogicalOr 复用参数化 CPU arithmetic dispatcher；dtype 和 operation parameter 决定具体操作。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49)。

```cpp
    49  int ArithmeticCPUKernel::Run() {
    50    if (in_tensors_[FIRST_INPUT]->data_type() != in_tensors_[SECOND_INPUT]->data_type()) {
    51      MS_LOG(ERROR) << "The dataTypes of input0 and input1 should be the same. input0: "
    52                    << in_tensors_[FIRST_INPUT]->data_type() << " vs input1" << in_tensors_[SECOND_INPUT]->data_type();
    53      return RET_ERROR;
    54    }
    55    if (op_parameter_->is_train_session_) {
    56      in_data_size_ = static_cast<int>(lite::DataTypeSize(in_tensors_.front()->data_type()));
    57    }
    58    if (!a_matric_.is_valid) {
    59      a_matric_.data = in_tensors_[FIRST_INPUT]->data();
    60    }
    61    if (!b_matric_.is_valid) {
    62      b_matric_.data = in_tensors_[SECOND_INPUT]->data();
    63    }
    64    c_matric_.data = out_tensors_.front()->data();
    65    return ArithmeticBaseCPUKernel::Run();
    66  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ArithmeticCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；18 行，35 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=6，指针=0，赋值=4，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ArithmeticBaseCPUKernel::Run` x1 (L65)；`MS_LOG` x1 (L51)；`in_tensors_.front` x1 (L56)；`in_tensors_.front()->data_type` x1 (L56)；`in_tensors_[FIRST_INPUT]->data` x1 (L59)；`in_tensors_[FIRST_INPUT]->data_type` x2 (L50,52)；`in_tensors_[SECOND_INPUT]->data` x1 (L62)；`in_tensors_[SECOND_INPUT]->data_type` x2 (L50,52)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f62958496ab6cdbe931737725a407adf7566399e5e33779455b665653282803`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_logical_or` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.32 Kirin NPU（HiAI DDK）：mindspore_mat_mul_fusion

**严格 pair**：`mindspore_kirin_npu_mat_mul_fusion_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::MatMulNPUOp for PrimitiveType_MatMulFusion`。
- **CPU/software 回退**：`MatmulFP16CPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：FP16 MatMul Run 委托给 base kernel；packing、batch loop、线程与矩阵 MAC 调用在入口之下。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp16/matmul_fp16.cc:90](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp16/matmul_fp16.cc:90)。

```cpp
    90  int MatmulFP16CPUKernel::Run() {
    91    if (IsTrainable() && (IsTrain())) {
    92      is_repack_ = true;
    93    }
    94    auto ret = MatmulBaseFP16CPUKernel::Run();
    95    if (ret != RET_OK) {
    96      MS_LOG(ERROR) << "MatmulFP16CPUKernel run failed";
    97    }
    98    is_repack_ = false;
    99    return ret;
   100  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `MatmulFP16CPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp16/matmul_fp16.cc:90` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；11 行，13 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=2，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=0，赋值=2，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `IsTrain` x1 (L91)；`IsTrainable` x1 (L91)；`MS_LOG` x1 (L96)；`MatmulBaseFP16CPUKernel::Run` x1 (L94) | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；无 | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `0ddb1b4ceea0f6b8a941d14b9153e90d68078d6cc2a50e49876486cf57d617b1`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_mat_mul_fusion` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.33 Kirin NPU（HiAI DDK）：mindspore_maximum

**严格 pair**：`mindspore_kirin_npu_maximum_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::ArithmeticNPUOp for PrimitiveType_Maximum`。
- **CPU/software 回退**：`ArithmeticCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：Maximum 复用参数化 CPU arithmetic dispatcher；具体逐元素循环在 Run 之下。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49)。

```cpp
    49  int ArithmeticCPUKernel::Run() {
    50    if (in_tensors_[FIRST_INPUT]->data_type() != in_tensors_[SECOND_INPUT]->data_type()) {
    51      MS_LOG(ERROR) << "The dataTypes of input0 and input1 should be the same. input0: "
    52                    << in_tensors_[FIRST_INPUT]->data_type() << " vs input1" << in_tensors_[SECOND_INPUT]->data_type();
    53      return RET_ERROR;
    54    }
    55    if (op_parameter_->is_train_session_) {
    56      in_data_size_ = static_cast<int>(lite::DataTypeSize(in_tensors_.front()->data_type()));
    57    }
    58    if (!a_matric_.is_valid) {
    59      a_matric_.data = in_tensors_[FIRST_INPUT]->data();
    60    }
    61    if (!b_matric_.is_valid) {
    62      b_matric_.data = in_tensors_[SECOND_INPUT]->data();
    63    }
    64    c_matric_.data = out_tensors_.front()->data();
    65    return ArithmeticBaseCPUKernel::Run();
    66  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ArithmeticCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；18 行，35 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=6，指针=0，赋值=4，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ArithmeticBaseCPUKernel::Run` x1 (L65)；`MS_LOG` x1 (L51)；`in_tensors_.front` x1 (L56)；`in_tensors_.front()->data_type` x1 (L56)；`in_tensors_[FIRST_INPUT]->data` x1 (L59)；`in_tensors_[FIRST_INPUT]->data_type` x2 (L50,52)；`in_tensors_[SECOND_INPUT]->data` x1 (L62)；`in_tensors_[SECOND_INPUT]->data_type` x2 (L50,52)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f62958496ab6cdbe931737725a407adf7566399e5e33779455b665653282803`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_maximum` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.34 Kirin NPU（HiAI DDK）：mindspore_minimum

**严格 pair**：`mindspore_kirin_npu_minimum_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::ArithmeticNPUOp for PrimitiveType_Minimum`。
- **CPU/software 回退**：`ArithmeticCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：Minimum 复用参数化 CPU arithmetic dispatcher；具体逐元素循环在 Run 之下。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49)。

```cpp
    49  int ArithmeticCPUKernel::Run() {
    50    if (in_tensors_[FIRST_INPUT]->data_type() != in_tensors_[SECOND_INPUT]->data_type()) {
    51      MS_LOG(ERROR) << "The dataTypes of input0 and input1 should be the same. input0: "
    52                    << in_tensors_[FIRST_INPUT]->data_type() << " vs input1" << in_tensors_[SECOND_INPUT]->data_type();
    53      return RET_ERROR;
    54    }
    55    if (op_parameter_->is_train_session_) {
    56      in_data_size_ = static_cast<int>(lite::DataTypeSize(in_tensors_.front()->data_type()));
    57    }
    58    if (!a_matric_.is_valid) {
    59      a_matric_.data = in_tensors_[FIRST_INPUT]->data();
    60    }
    61    if (!b_matric_.is_valid) {
    62      b_matric_.data = in_tensors_[SECOND_INPUT]->data();
    63    }
    64    c_matric_.data = out_tensors_.front()->data();
    65    return ArithmeticBaseCPUKernel::Run();
    66  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ArithmeticCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；18 行，35 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=6，指针=0，赋值=4，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ArithmeticBaseCPUKernel::Run` x1 (L65)；`MS_LOG` x1 (L51)；`in_tensors_.front` x1 (L56)；`in_tensors_.front()->data_type` x1 (L56)；`in_tensors_[FIRST_INPUT]->data` x1 (L59)；`in_tensors_[FIRST_INPUT]->data_type` x2 (L50,52)；`in_tensors_[SECOND_INPUT]->data` x1 (L62)；`in_tensors_[SECOND_INPUT]->data_type` x2 (L50,52)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f62958496ab6cdbe931737725a407adf7566399e5e33779455b665653282803`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_minimum` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.35 Kirin NPU（HiAI DDK）：mindspore_mul_fusion

**严格 pair**：`mindspore_kirin_npu_mul_fusion_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::ArithmeticNPUOp for PrimitiveType_MulFusion`。
- **CPU/software 回退**：`ArithmeticCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：逐元素乘法复用 CPU arithmetic dispatcher；具体逐元素循环在 Run 之下。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49)。

```cpp
    49  int ArithmeticCPUKernel::Run() {
    50    if (in_tensors_[FIRST_INPUT]->data_type() != in_tensors_[SECOND_INPUT]->data_type()) {
    51      MS_LOG(ERROR) << "The dataTypes of input0 and input1 should be the same. input0: "
    52                    << in_tensors_[FIRST_INPUT]->data_type() << " vs input1" << in_tensors_[SECOND_INPUT]->data_type();
    53      return RET_ERROR;
    54    }
    55    if (op_parameter_->is_train_session_) {
    56      in_data_size_ = static_cast<int>(lite::DataTypeSize(in_tensors_.front()->data_type()));
    57    }
    58    if (!a_matric_.is_valid) {
    59      a_matric_.data = in_tensors_[FIRST_INPUT]->data();
    60    }
    61    if (!b_matric_.is_valid) {
    62      b_matric_.data = in_tensors_[SECOND_INPUT]->data();
    63    }
    64    c_matric_.data = out_tensors_.front()->data();
    65    return ArithmeticBaseCPUKernel::Run();
    66  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ArithmeticCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；18 行，35 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=6，指针=0，赋值=4，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ArithmeticBaseCPUKernel::Run` x1 (L65)；`MS_LOG` x1 (L51)；`in_tensors_.front` x1 (L56)；`in_tensors_.front()->data_type` x1 (L56)；`in_tensors_[FIRST_INPUT]->data` x1 (L59)；`in_tensors_[FIRST_INPUT]->data_type` x2 (L50,52)；`in_tensors_[SECOND_INPUT]->data` x1 (L62)；`in_tensors_[SECOND_INPUT]->data_type` x2 (L50,52)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f62958496ab6cdbe931737725a407adf7566399e5e33779455b665653282803`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_mul_fusion` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.36 Kirin NPU（HiAI DDK）：mindspore_resize

**严格 pair**：`mindspore_kirin_npu_resize_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::ResizeNPUOp for PrimitiveType_Resize`。
- **CPU/software 回退**：`ResizeCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：Resize Run 委托给按参数选择的 interpolation 代码；图像/张量遍历在入口之下。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/resize_fp32.cc:222](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/resize_fp32.cc:222)。

```cpp
   222  int ResizeCPUKernel::Run() {
   223    int error_code = ParallelLaunch(this->ms_context_, ResizeImpl, this, op_parameter_->thread_num_);
   224    if (error_code != RET_OK) {
   225      MS_LOG(ERROR) << "Resize run error, error_code[" << error_code << "]";
   226      FreeTmpBuffer();
   227      return RET_ERROR;
   228    }
   229    return RET_OK;
   230  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ResizeCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/resize_fp32.cc:222` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；9 行，11 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：int | 数据类型与接口边界 |
| 控制流 | 循环：无；if=1，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=0，赋值=0，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `FreeTmpBuffer` x1 (L226)；`MS_LOG` x1 (L225)；`ParallelLaunch` x1 (L223) | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`resize` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `86704f2734f02603cd6f7e5dfef28121070789ce1f409c0c44b3e1b47917187a`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_resize` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.37 Kirin NPU（HiAI DDK）：mindspore_split

**严格 pair**：`mindspore_kirin_npu_split_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::SplitNPUOp for PrimitiveType_Split`。
- **CPU/software 回退**：`SplitBaseCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：Split Run 负责校验与调度张量分片；buffer copy/index 核心委托给下层。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/base/split_base.cc:155](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/base/split_base.cc:155)。

```cpp
   155  int SplitBaseCPUKernel::Run() {
   156    auto input_tensor = in_tensors_.at(0);
   157    input_ptr_ = input_tensor->data();
   158    if (input_ptr_ == nullptr) {
   159      return RET_NULL_PTR;
   160    }
   161  
   162    for (int i = 0; i < param->num_split_; i++) {
   163      auto output_tensor = out_tensors_.at(i);
   164      output_ptr_.at(i) = output_tensor->data();
   165      if (output_ptr_.at(i) == nullptr) {
   166        return RET_NULL_PTR;
   167      }
   168    }
   169  
   170    auto ret = ParallelLaunch(this->ms_context_, SplitRun, this, thread_num_);
   171    if (ret != RET_OK) {
   172      MS_LOG(ERROR) << "split error error_code[" << ret << "]";
   173    }
   174    return ret;
   175  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `SplitBaseCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/base/split_base.cc:155` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；21 行，28 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：int | 数据类型与接口边界 |
| 控制流 | 循环：for_statement@L162 depth=1；if=3，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=0，指针=0，赋值=2，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `MS_LOG` x1 (L172)；`ParallelLaunch` x1 (L170)；`in_tensors_.at` x1 (L156)；`input_tensor->data` x1 (L157)；`out_tensors_.at` x1 (L163)；`output_ptr_.at` x2 (L164,165)；`output_tensor->data` x1 (L164) | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`iterative` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `57dad39de240ff96e1170c00e85d0826a86471f5cd2ea90d45e55adcb693a74e`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_split` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.38 Kirin NPU（HiAI DDK）：mindspore_sub_fusion

**严格 pair**：`mindspore_kirin_npu_sub_fusion_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::ArithmeticNPUOp for PrimitiveType_SubFusion`。
- **CPU/software 回退**：`ArithmeticCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：逐元素减法复用 CPU arithmetic dispatcher；具体逐元素循环在 Run 之下。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49)。

```cpp
    49  int ArithmeticCPUKernel::Run() {
    50    if (in_tensors_[FIRST_INPUT]->data_type() != in_tensors_[SECOND_INPUT]->data_type()) {
    51      MS_LOG(ERROR) << "The dataTypes of input0 and input1 should be the same. input0: "
    52                    << in_tensors_[FIRST_INPUT]->data_type() << " vs input1" << in_tensors_[SECOND_INPUT]->data_type();
    53      return RET_ERROR;
    54    }
    55    if (op_parameter_->is_train_session_) {
    56      in_data_size_ = static_cast<int>(lite::DataTypeSize(in_tensors_.front()->data_type()));
    57    }
    58    if (!a_matric_.is_valid) {
    59      a_matric_.data = in_tensors_[FIRST_INPUT]->data();
    60    }
    61    if (!b_matric_.is_valid) {
    62      b_matric_.data = in_tensors_[SECOND_INPUT]->data();
    63    }
    64    c_matric_.data = out_tensors_.front()->data();
    65    return ArithmeticBaseCPUKernel::Run();
    66  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `ArithmeticCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/arithmetic_fp32.cc:49` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；18 行，35 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：无 | 数据类型与接口边界 |
| 控制流 | 循环：无；if=4，switch=0，ternary=0 | 区分调度循环与数据并行循环 |
| 访存 | 下标=6，指针=0，赋值=4，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `ArithmeticBaseCPUKernel::Run` x1 (L65)；`MS_LOG` x1 (L51)；`in_tensors_.front` x1 (L56)；`in_tensors_.front()->data_type` x1 (L56)；`in_tensors_[FIRST_INPUT]->data` x1 (L59)；`in_tensors_[FIRST_INPUT]->data_type` x2 (L50,52)；`in_tensors_[SECOND_INPUT]->data` x1 (L62)；`in_tensors_[SECOND_INPUT]->data_type` x2 (L50,52)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `5f62958496ab6cdbe931737725a407adf7566399e5e33779455b665653282803`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_sub_fusion` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.39 Kirin NPU（HiAI DDK）：mindspore_unstack

**严格 pair**：`mindspore_kirin_npu_unstack_to_cpu_kernel_fallback`。

- **异构单元**：Kirin NPU（HiAI DDK），实现路径为 `NPUDelegate::UnpackNPUOp for PrimitiveType_Unstack`。
- **CPU/software 回退**：`UnstackCPUKernel::Run`；当前绑定状态 `exact_definition`。
- **代码语义**：Unstack Run 遍历 output tensor 并委托 slice copy；tensor offset 与 copy 实现仍需 callee 证据。

**CPU 代码（精确函数定义）**：[openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/unstack_fp32.cc:73](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/unstack_fp32.cc:73)。

```cpp
    73  int UnstackCPUKernel::Run() {
    74    void *input_data = in_tensors_.front()->MutableData();
    75    CHECK_NULL_RETURN(input_data);
    76    size_t out_num = out_tensors_.size();
    77    for (size_t i = 0; i < out_num; i++) {
    78      output_addr_array_[i] = out_tensors_.at(i)->data();
    79      CHECK_NULL_RETURN(output_addr_array_[i]);
    80    }
    81    auto para = reinterpret_cast<UnstackParameter *>(op_parameter_);
    82    para->num_ = static_cast<int>(out_num);
    83    int data_type_len = in_tensors_.front()->data_type() == kNumberTypeFloat16 ? FP16_DATA_TYPE_LEN : sizeof(float);
    84    Unstack(input_data, output_addr_array_, para, data_type_len);
    85    return RET_OK;
    86  }
```

**函数级 AST 特征**：

| AST 特征项 | 提取结果 | 工程解释 |
| --- | --- | --- |
| 精确绑定 | `UnstackCPUKernel::Run`；`openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/kernel/cpu/fp32/unstack_fp32.cc:73` | 函数范围与源文件 SHA 已记录在 profile |
| 解析与规模 | `tree_sitter_cpp` / `complete`；14 行，26 个计数节点 | 仅是语法 AST，不是编译或跨过程语义 |
| 参数/局部类型 | 无；局部类型：int, size_t, void | 数据类型与接口边界 |
| 控制流 | 循环：for_statement@L77 depth=1；if=0，switch=0，ternary=1 | 区分调度循环与数据并行循环 |
| 访存 | 下标=2，指针=0，赋值=2，内存库调用=0 | 只描述本函数体可见的访存 |
| 调用 | `CHECK_NULL_RETURN` x2 (L75,79)；`Unstack` x1 (L84)；`in_tensors_.front` x2 (L74,83)；`in_tensors_.front()->MutableData` x1 (L74)；`in_tensors_.front()->data_type` x1 (L83)；`out_tensors_.at` x1 (L78)；`out_tensors_.at(i)->data` x1 (L78)；`out_tensors_.size` x1 (L76)；其余见 JSON | 被调函数可能才是主计算核心 |
| 并行/标签 | OpenMP=0；`buffer_access`, `iterative` | 标签用于检索，不是硬件可行性结论 |
| 指纹与核心门禁 | `30a115a48806c954f21a7bff8cf57a88f64b40e04bcce74615c50d887ff03732`；`evidence_required/not_ready` | 函数已精确绑定并可解析，但尚未完成调用图或函数体主计算审计，不能参与热点相似匹配 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`mindspore_unstack` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

### 5.40 Kirin NPU（OpenHarmony NNRT）：inference_subgraph

**严格 pair**：`mindspore_nnrt_kirin_npu_to_cpu_subgraph_fallback`。

- **异构单元**：Kirin NPU（OpenHarmony NNRT），实现路径为 `NNRTDelegate::BuildNormalModel -> OH_NNCompilation / OH_NNExecutor`。
- **CPU/software 回退**：`original CPU KernelExec retained by Scheduler::ReplaceDelegateKernels`；当前绑定状态 `runtime_selected_original_cpu_kernel`。
- **代码语义**：NNRT 回退保留模型原有 CPU KernelExec 集合；需要模型/runtime trace 才能命名具体函数。

**CPU 回退控制流证据（非唯一算法函数）**：

```cpp
   553      size_t index = 0;
   554      for (; index < src_kernels.size(); index++) {
   555        if (kernel == src_kernels[index]->kernel()) {
   556          // Kernels that the delegate does not support keep the original backend
   557          dst_kernels->push_back(src_kernels[index]);
   558          delegate_support[src_kernels[index]] = false;
   559          break;
   560        }
   561      }
   562      if (index == src_kernels.size()) {
   563        // New liteKernel to save delegate subgraph
   564        std::shared_ptr<kernel::Kernel> shared_kernel(kernel);
   565        auto kernel_exec = new (std::nothrow) kernel::KernelExec(shared_kernel);
   566        if (kernel_exec == nullptr) {
```

**AST / 绑定结论**：

| AST / 绑定项 | 结果 | 工程含义 |
| --- | --- | --- |
| 绑定状态 | `runtime_selected_original_cpu_kernel` | 回退保留模型原有 CPU kernel 集合；具体函数由模型和运行时决定。 |
| 请求符号 | `Scheduler::ReplaceDelegateKernels`, `original` | 这些符号描述路径，不等于可安全分析的算法函数 |
| 控制流证据 | [openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/scheduler.cc:557](../openharmony/third_party/mindspore/mindspore-src/source/mindspore-lite/src/litert/scheduler.cc:557) | 展示真实回退边界 |
| 候选定义 | 无唯一函数定义 | 候选、factory 或 dispatcher 不可替代实际 CPU 算法 AST |
| AST 结论 | 未生成函数级算法 AST | 必须结合 trace、runtime descriptor、provider/codec 解析或模型实例继续下钻 |

- **异构映射与边界**：映射来自同一 MindSpore primitive 的 NPU delegate 与 CPU fallback 注册，而非名称相似。`inference_subgraph` 仍需验证 shape、dtype、layout、量化语义、设备可用性和传输成本；入口 wrapper 的 AST 不足以证明 NPU 收益。

## 6. 使用约束

1. 先由 trace 的单次/窗口 self time、调用频率和关键路径确认候选热点；代码规模或累计时间不能替代热点证据。
2. 仅 `compute_core.matching_status=ready` 的 profile 可参与热点 AST 相似匹配。当前 29 个精确定义均需完成函数体或已解析 callee 的主计算审计。
3. 相似性只提出已存在异构单元家族的候选，仍须验证设备可用性、正确性、shape/dtype/格式、复制同步成本与性能收益。
4. runtime provider、factory、外部 codec 与 NNRT 子图应由运行时实例继续绑定，不能用 dispatcher AST 取代算法 AST。

## 7. 可审计资产

| 资产 | 用途 |
| --- | --- |
| [v21 pair archive](tools/heterogeneous_kb/pair_discovery/openharmony_source_confirmed_cpu_fallback_pairs_v21.json) | 本文选取其中 17 条 Foundation/ArkUI/媒体/视频路径。 |
| [v37 Audio DSP pair](tools/heterogeneous_kb/pair_discovery/openharmony_source_confirmed_framework_additions_v37.json) | DSP HDI 到 AP runtime effect chain。 |
| [v38 MindSpore NPU/NNRT pair](tools/heterogeneous_kb/pair_discovery/openharmony_source_confirmed_mindspore_npu_cpu_pairs_v38.json) | 21 条 HiAI DDK primitive 与 1 条 NNRT 子图。 |
| [v1/v37/v38 AST profile](tools/heterogeneous_kb/pair_discovery/) | 函数级 AST、绑定状态与计算核心门禁。 |
| [Audio Effect dispatcher AST evidence](tools/heterogeneous_kb/pair_discovery/audio_effect_chain_apply_effect_chain_ast_features_v1.json) | runtime effect-chain dispatcher 的独立 AST 证据。 |
