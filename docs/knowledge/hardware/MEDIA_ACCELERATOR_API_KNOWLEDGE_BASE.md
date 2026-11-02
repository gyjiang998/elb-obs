# OpenHarmony 媒体硬件加速 API 知识库

> 自动生成：`python3 docs_ffrt/tools/build_accelerator_api_knowledge_bases.py`。不要手工编辑生成文件。

## 定位与边界

公开 AVCodec NDK 的硬件/软件视频 codec 选择与生命周期入口，以及源码已证实的 HCodec Codec HDI、JPEG 硬件编码/解码、HEIF 硬件解码系统集成和 Image Codec HDI 合同。每条记录明确暴露层级；内部路径和 HDI 均不伪装为应用 NDK。

所有记录的目标设备可用性均为 `evidence_required`。接口/源码路径存在不等于设备驱动、格式、权限、实现或收益已经可用。

## 内容统计

- API/集成入口：31
- 同一硬件单元的严格 fallback 案例：10
- 与具体入口直接关联 strict case 的记录：18

## 运行时验证协议

1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

## API 目录

| API / 集成入口 | 语义族 | 暴露层级 | 一句话摘要 | 直接 strict case |
|---|---|---|---|---|
| `DecoderSurfaceFilter::CheckDecoderCapability` | 系统硬件视频解码选择与回退 | system_source_integration | 先检查 HARDWARE decoder capability，再检查 SOFTWARE decoder capability。 | video_decoder_surface_hardware_to_software_capability_fallback |
| `ExtDecoder::DoHardWareDecode` | JPEG 硬件解码与 Image Codec HDI | system_source_integration | 在 JPEG、allocator 与硬件能力门禁满足时调度 Image Codec 硬件解码。 | jpeg_hardware_decode_to_skia_fallback |
| `ExtEncoder::DoHardWareEncode` | JPEG 硬件编码系统路径 | system_source_integration | 通过 ImageFwkExtNativeSo 动态解析 DoHardwareEncode 并提交 PixelMap。 | jpeg_hardware_encode_to_skia_fallback |
| `ExtEncoder::HardwareEncode` | JPEG 硬件编码系统路径 | system_source_integration | 在 JPEG PixelMap 满足约束时优先委派内部硬件编码扩展。 | jpeg_hardware_encode_to_skia_fallback |
| `ExtEncoder::TryHardwareEncodePicture` | JPEG 硬件编码系统路径 | system_source_integration | 为带主图和辅助图的 Picture 请求内部硬件编码扩展。 | jpeg_picture_hardware_encode_to_software_fallback |
| `HCodec::Create` | HCodec 硬件视频能力发现 | system_source_integration | 根据 HDI component capability 创建 HDecoder 或 HEncoder 对象。 | avcodec_hcodec_to_fcodec_ordered_factory_fallback |
| `HCodec::OnAllocateComponent` | HCodec 硬件组件生命周期 | system_source_integration | 向 Codec Component HDI 创建已选定的硬件视频 component。 | avcodec_hcodec_to_fcodec_ordered_factory_fallback |
| `HCodecLoader::CreateByName` | HCodec 硬件组件生命周期 | system_source_integration | 按 capability 选择的名称加载系统 HCodec 实现。 | avcodec_hcodec_to_fcodec_ordered_factory_fallback |
| `HeifDecoderImpl::HwApplyAlphaImage` | HEIF 硬件解码与 Image Codec HDI | system_source_integration | 使用硬件路径解码 alpha image，再填充主图 alpha channel。 | heif_hardware_alpha_to_software_alpha_fallback |
| `HeifDecoderImpl::HwDecodeImage` | HEIF 硬件解码与 Image Codec HDI | system_source_integration | 将 HEIF grid 或 hvc1 数据组织为 HDI 输入并请求硬件输出 SurfaceBuffer。 | heif_hardware_auxiliary_decode_to_software_fallback, heif_hardware_primary_decode_to_software_fallback |
| `ICodecComponentManager::CreateComponent` | HCodec 硬件组件生命周期 | driver_hdi_contract | 按能力返回的 component 名称创建驱动 codec 组件及回调通道。 | avcodec_hcodec_to_fcodec_ordered_factory_fallback |
| `ICodecComponentManager::GetComponentCapabilityList` | HCodec 硬件视频能力发现 | driver_hdi_contract | 读取驱动提供的视频编解码 component 能力列表。 | avcodec_hcodec_to_fcodec_ordered_factory_fallback |
| `ICodecImage::DoHeifDecode` | HEIF 硬件解码与 Image Codec HDI | driver_hdi_contract | 将 HEIF Ashmem bitstream、NativeBuffer 输出和 CodecHeifDecInfo 提交给图像 codec 驱动服务。 | heif_hardware_auxiliary_decode_to_software_fallback, heif_hardware_primary_decode_to_software_fallback |
| `ICodecImage::DoJpegDecode` | JPEG 硬件解码与 Image Codec HDI | driver_hdi_contract | 将 JPEG 输入/输出 CodecImageBuffer 与解码参数提交给图像 codec 驱动服务。 | jpeg_hardware_decode_to_skia_fallback |
| `JpegHardwareDecoder::Decode` | JPEG 硬件解码与 Image Codec HDI | system_source_integration | 准备 JPEG 输入 buffer、检查 HDI capability 并调用 Image Codec HDI。 | jpeg_hardware_decode_to_skia_fallback |
| `MediaAVCodec::VideoDecoderFactory::CreateByMime` | 系统硬件视频解码选择与回退 | system_source_integration | 在缩略图等系统路径中按 MIME 创建硬件 decoder，并在回退时按软件能力名称创建 decoder。 | thumbnail_hardware_decoder_to_software_reinitialization |
| `OH_AVCodec_GetCapabilityByCategory` | 媒体硬件能力选择 | openharmony_ndk | 按 MIME、编解码方向和 HARDWARE/SOFTWARE 类别查询 codec capability。 | webview_video_decoder_hardware_to_software_creation_fallback |
| `OH_VideoDecoder_Configure` | 硬件视频解码生命周期 | openharmony_ndk | 以 OH_AVFormat 配置 decoder 的实际输入输出参数。 | 无 |
| `OH_VideoDecoder_CreateByMime` | 硬件视频解码生命周期 | openharmony_ndk | 基于 MIME 创建视频解码器实例。 | 无 |
| `OH_VideoDecoder_CreateByName` | 硬件视频解码生命周期 | openharmony_ndk | 使用 capability 查询返回的准确名称创建视频解码器。 | webview_video_decoder_hardware_to_software_creation_fallback |
| `OH_VideoDecoder_Destroy` | 硬件视频解码生命周期 | openharmony_ndk | 释放 decoder 内部资源与实例。 | 无 |
| `OH_VideoDecoder_Prepare` | 硬件视频解码生命周期 | openharmony_ndk | 完成 decoder 初始化并进入可启动状态。 | 无 |
| `OH_VideoDecoder_Start` | 硬件视频解码生命周期 | openharmony_ndk | 开始 decoder 的异步/缓冲处理。 | 无 |
| `OH_VideoDecoder_Stop` | 硬件视频解码生命周期 | openharmony_ndk | 停止 decoder 并结束当前处理会话。 | 无 |
| `OH_VideoEncoder_Configure` | 硬件视频编码生命周期 | openharmony_ndk | 以 OH_AVFormat 配置 encoder 的实际格式与编码参数。 | 无 |
| `OH_VideoEncoder_CreateByMime` | 硬件视频编码生命周期 | openharmony_ndk | 基于 MIME 创建视频编码器实例。 | 无 |
| `OH_VideoEncoder_CreateByName` | 硬件视频编码生命周期 | openharmony_ndk | 使用 capability 查询返回的准确名称创建视频编码器。 | 无 |
| `OH_VideoEncoder_Destroy` | 硬件视频编码生命周期 | openharmony_ndk | 释放 encoder 内部资源与实例。 | 无 |
| `OH_VideoEncoder_Prepare` | 硬件视频编码生命周期 | openharmony_ndk | 完成 encoder 初始化并进入可启动状态。 | 无 |
| `OH_VideoEncoder_Start` | 硬件视频编码生命周期 | openharmony_ndk | 开始 encoder 的异步/缓冲处理。 | 无 |
| `OH_VideoEncoder_Stop` | 硬件视频编码生命周期 | openharmony_ndk | 停止 encoder 并结束当前处理会话。 | 无 |

## 详细记录

### `DecoderSurfaceFilter::CheckDecoderCapability`

先检查 HARDWARE decoder capability，再检查 SOFTWARE decoder capability。

DecoderSurfaceFilter 以 MIME、宽高和 profile 验证硬件 decoder；硬件不满足时调用相同逻辑检查软件 decoder。这是严格硬件到软件能力回退的系统实现证据，而不是应用侧公开 API。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/av_codec/services/media_engine/filters/decoder_surface_filter.cpp:366`；库：AVCodec media engine internal filter；SystemCapability：system media service integration。
- **代码/AST 信号**：video decoder/codec MIME；HARDWARE/SOFTWARE capability 分支；width/height、profile、surface 或 AVFormat；创建失败后 software decoder 回退。
- **数据形态**：视频 MIME/码流；width/height/profile；AVFormat、SurfaceBuffer 或 decoder callback。
- **Top-N 匹配别名**：checkdecodercapability, decodersurfacefilter_checkdecodercapability, hardware_software_decoder。
- **单独卸载边界**：这是系统实现选择逻辑；业务代码应优先通过公开 AVCodec capability 与生命周期 API 获取同等语义，不应依赖 internal filter/factory 符号。
- **直接 strict case**：video_decoder_surface_hardware_to_software_capability_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 以真实 MIME、profile、width/height 查询 HARDWARE decoder capability，并记录不支持原因。
  2. 硬件不满足时验证 SOFTWARE capability、decoder 名称和后续 Init/Configure/Start。
  3. 使用公开 AVCodec NDK 时复现同样的能力选择和软件回退语义，不直接调用 internal filter。

### `ExtDecoder::DoHardWareDecode`

在 JPEG、allocator 与硬件能力门禁满足时调度 Image Codec 硬件解码。

ExtDecoder 仅在 JPEG_HW_DECODE_ENABLE、系统开关、JPEG 格式、allocator 和 JpegHardwareDecoder capability 均通过时调用该入口；失败后继续 SkCodec::getPixels 等软件解码路径。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_decoder.cpp:1204`；库：image framework internal decoder；SystemCapability：system image framework integration。
- **代码/AST 信号**：JPEG/JPG 解码；压缩 bitstream 与输出 PixelMap/NativeBuffer；sample size、颜色格式、宽高/对齐或 DMA buffer；ICodecImage/DoJpegDecode 与软件解码回退。
- **数据形态**：CodecImageBuffer 输入/输出；CodecJpegDecInfo；DMA/NativeBuffer、sample size、YUV/RGBA 输出。
- **Top-N 匹配别名**：dohardwaredecode, hardware_decode_jpeg, jpeg_hw_decode。
- **单独卸载边界**：ICodecImage 是系统/驱动 HDI 合同；调用侧还必须满足实际 capability、buffer allocator、对齐、颜色格式和服务可用性，不能视为普通 NDK。
- **直接 strict case**：jpeg_hardware_decode_to_skia_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 验证 JPEG_HW_DECODE_ENABLE、系统开关、JPEG bitstream、allocator/DMA 条件、尺寸和对齐约束。
  2. 验证 InitDecoder、DoJpegDecode、色彩空间处理和输出 buffer 更新均成功。
  3. 失败后验证 Skia Decode/getPixels 仍提供等价输出，并测量准备/复制/回退成本。

### `ExtEncoder::DoHardWareEncode`

通过 ImageFwkExtNativeSo 动态解析 DoHardwareEncode 并提交 PixelMap。

该入口是当前源码可见的 JPEG 硬件编码委派边界：dlsym 到内部 native extension 后传入 SkWStream、编码选项和 PixelMap。工作区未提供该二进制 ABI 的应用侧公开合同，因此它只能作为系统实现参考。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_encoder.cpp:600`；库：image framework internal native extension；SystemCapability：system image framework integration。
- **代码/AST 信号**：JPEG/JPG 编码；PixelMap/Bitmap/图像像素数据；NV12/NV21/RGBA 与 width/height/quality；硬件优先并有软件编码回退。
- **数据形态**：image/jpeg；NV12、NV21 或受约束 RGBA PixelMap；偶数宽高、质量、输出 stream/EXIF。
- **Top-N 匹配别名**：dohardwareencode, extencoder_dohardwareencode, image_fwk_ext_native_so。
- **单独卸载边界**：当前可见源码通过 ImageFwkExtNativeSo 的 dlsym 委派编码；未在工作区建立可供普通应用直接调用的 JPEG 编码 HDI/NDK 合同。
- **直接 strict case**：jpeg_hardware_encode_to_skia_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 验证 ImageFwkExtNativeSo 可加载且 DoHardwareEncode 符号存在；这不是应用侧 SDK 可用性证明。
  2. 对 PixelMap 格式、尺寸、quality、输出 stream、返回码和故障 telemetry 建立证据。
  3. delegate 加载或编码失败时验证 ExtEncoder 的 Skia/软件回退完整执行。

### `ExtEncoder::HardwareEncode`

在 JPEG PixelMap 满足约束时优先委派内部硬件编码扩展。

ExtEncoder 仅对 image/jpeg、质量、偶数尺寸、NV12/NV21 或受约束 RGBA PixelMap 等条件开启该路径；硬件调用失败后上层继续走 Hispeed/Skia 软件编码。该符号属于 Image Framework 内部实现。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_encoder.cpp:698`；库：image framework internal integration；SystemCapability：system image framework integration。
- **代码/AST 信号**：JPEG/JPG 编码；PixelMap/Bitmap/图像像素数据；NV12/NV21/RGBA 与 width/height/quality；硬件优先并有软件编码回退。
- **数据形态**：image/jpeg；NV12、NV21 或受约束 RGBA PixelMap；偶数宽高、质量、输出 stream/EXIF。
- **Top-N 匹配别名**：hardware_encode_jpeg, hardwareencode, image_jpeg_hardware_encode。
- **单独卸载边界**：当前可见源码通过 ImageFwkExtNativeSo 的 dlsym 委派编码；未在工作区建立可供普通应用直接调用的 JPEG 编码 HDI/NDK 合同。
- **直接 strict case**：jpeg_hardware_encode_to_skia_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 验证 image/jpeg、quality、偶数宽高、PixelMap format 和 ImageSystemProperties 开关满足硬件路径要求。
  2. 验证内部 delegate 完成输出并且 EXIF/stream 处理正确；失败必须继续软件编码。
  3. 在包含格式转换、委派加载和软件回退的端到端编码 workload 上测量吞吐、时延和功耗。

### `ExtEncoder::TryHardwareEncodePicture`

为带主图和辅助图的 Picture 请求内部硬件编码扩展。

相机场景会先检查主图/辅助图尺寸、质量和系统开关，然后委派 DoHardwareEncodePicture；JPEG 委派失败时明确调用 EncodeJpegPicture 软件路径。该路径不建立普通应用可直接调用的硬件 JPEG API。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/ext_encoder.cpp:2447`；库：image framework internal native extension；SystemCapability：system image framework integration。
- **代码/AST 信号**：JPEG/JPG 编码；PixelMap/Bitmap/图像像素数据；NV12/NV21/RGBA 与 width/height/quality；硬件优先并有软件编码回退。
- **数据形态**：image/jpeg；NV12、NV21 或受约束 RGBA PixelMap；偶数宽高、质量、输出 stream/EXIF。
- **Top-N 匹配别名**：hardware_encode_picture, jpeg_picture_encode, tryhardwareencodepicture。
- **单独卸载边界**：当前可见源码通过 ImageFwkExtNativeSo 的 dlsym 委派编码；未在工作区建立可供普通应用直接调用的 JPEG 编码 HDI/NDK 合同。
- **直接 strict case**：jpeg_picture_hardware_encode_to_software_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 验证 Picture 主图/辅助图、JPEG 质量、偶数尺寸和系统硬件编码开关。
  2. 验证 DoHardwareEncodePicture delegate 可加载、输出元数据正确并记录具体失败原因。
  3. 硬件 Picture 编码失败时验证 EncodeJpegPicture 软件回退和输出兼容性。

### `HCodec::Create`

根据 HDI component capability 创建 HDecoder 或 HEncoder 对象。

HCodec::Create 遍历当前设备的 CodecCompCapability，按 component 名称和 role 将视频 decoder/encoder 映射为 HDecoder/HEncoder。这证明 HCodec 是 HDI component 的系统封装，不是面向业务代码的独立硬件 API。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/av_codec/services/engine/codec/video/hcodec/hcodec.cpp:55`；库：AVCodec service internal HCodec；SystemCapability：system media service integration。
- **代码/AST 信号**：HCodec/CodecComponentManager/Capability 语义；codec MIME、role、profile、width/height 或 pixel format；硬件 component 名称与 capability 查询；硬件优先、软件 factory 回退控制流。
- **数据形态**：CodecCompCapability；MIME/role/profile/level；宽高、对齐、帧率、bitrate、pixel format。
- **Top-N 匹配别名**：codeccompcapability, hcodeccreate, hdecoder, hencoder。
- **单独卸载边界**：HCodec/HDI 能力项来自设备运行时；源码中的 component 名称、枚举接口或 HCodec factory 不能作为某设备支持某格式的证明。
- **直接 strict case**：avcodec_hcodec_to_fcodec_ordered_factory_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 读取实际 CodecCompCapability，确认 component 名称、VIDEO_DECODER/VIDEO_ENCODER role、MIME/profile/size/format 约束。
  2. 验证 HCodec::Create 返回的对象可完成 Init、Configure、Start，且应用实例限制没有触发。
  3. 任一 capability 或生命周期门禁失败时转向 FCodec/software，比较功能和端到端时延。

### `HCodec::OnAllocateComponent`

向 Codec Component HDI 创建已选定的硬件视频 component。

HCodec 在建立 callback、passthrough/IPC 模式、安全标志和实例计数约束后调用 ICodecComponentManager::CreateComponent。它是实际 HCodec 到 HDI 的创建桥梁，处理失败并不意味着可继续将任务视为硬件执行。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/av_codec/services/engine/codec/video/hcodec/hcodec.cpp:1514`；库：AVCodec service internal HCodec；SystemCapability：system media service integration。
- **代码/AST 信号**：HCodec/CreateComponent/ICodecComponent；输入输出 buffer 或 Surface；codec callback 与状态机；video encode/decode 生命周期。
- **数据形态**：CodecCompCapability 和 componentId；compressed bitstream 或 raw frame；SurfaceBuffer、DMA buffer、fence 与 callback。
- **Top-N 匹配别名**：createcomponent, hcodec_hdi, onallocatecomponent。
- **单独卸载边界**：组件创建与 buffer 生命周期属于系统媒体栈/HDI 集成；普通热点不能绕过 AVCodec API 直接调用它，且必须保留 FCodec/software 回退。
- **直接 strict case**：avcodec_hcodec_to_fcodec_ordered_factory_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 验证 component manager、callback、security/passthrough 模式、实例上限及 componentId 创建结果。
  2. 验证输入输出 buffer 或 Surface、fence、回调和状态机在真实码流下工作。
  3. CreateComponent 或后续状态转换失败时释放资源并执行 FCodec/software 回退。

### `HCodecLoader::CreateByName`

按 capability 选择的名称加载系统 HCodec 实现。

CodecFactory 将识别为 AVCODEC_HCODEC 的名称分派给 HCodecLoader；loader 动态加载 libhcodec.z.so 并调用 CreateHCodecByName。该路径是系统 codec factory 的内部集成，失败后同一 MIME 可继续尝试 FCodec/software factory。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/av_codec/services/engine/codec/video/hcodec_loader.cpp:28`；库：AVCodec service internal HCodec loader；SystemCapability：system media service integration。
- **代码/AST 信号**：HCodec/CreateComponent/ICodecComponent；输入输出 buffer 或 Surface；codec callback 与状态机；video encode/decode 生命周期。
- **数据形态**：CodecCompCapability 和 componentId；compressed bitstream 或 raw frame；SurfaceBuffer、DMA buffer、fence 与 callback。
- **Top-N 匹配别名**：avcodec_hcodec, createhcodecbyname, hcodecloader。
- **单独卸载边界**：组件创建与 buffer 生命周期属于系统媒体栈/HDI 集成；普通热点不能绕过 AVCodec API 直接调用它，且必须保留 FCodec/software 回退。
- **直接 strict case**：avcodec_hcodec_to_fcodec_ordered_factory_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 从当前设备 capability 获取 codec 名称，不能使用源码中的 HCodec 名称作为跨设备硬件事实。
  2. 验证 libhcodec.z.so、CreateHCodecByName、Init/Configure/Start 和 service 生命周期均成功。
  3. HCodec 初始化/配置/启动失败时验证同 MIME FCodec/software factory 的顺序回退和功能一致性。

### `HeifDecoderImpl::HwApplyAlphaImage`

使用硬件路径解码 alpha image，再填充主图 alpha channel。

该入口先验证 alpha 图格式，再通过 HwDecodeImage 获取硬件输出并完成 alpha channel 合成；失败时在默认 sample size 下回退 SwApplyAlphaImage。它是 HEIF 特定系统逻辑，不是通用 GPU/DSP 图像算子。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp:1188`；库：image framework HEIF decoder；SystemCapability：system image framework integration。
- **代码/AST 信号**：HEIF/HEIC、grid 或 hvc1；HVCC/bitstream/Ashmem 输入；NativeBuffer/SurfaceBuffer、YUV/RGBA、tile 宽高；主图、辅助图或 alpha 合成与软件回退。
- **数据形态**：Ashmem[] bitstream；NativeBuffer 输出；CodecHeifDecInfo、grid 行列/tile、像素格式与色彩空间。
- **Top-N 匹配别名**：fillalphachannel, heif_alpha_hardware_decode, hwapplyalphaimage。
- **单独卸载边界**：该 HDI 合同只证明系统图像框架存在硬件路径；格式、grid 布局、sample size、色彩空间、service/driver 和设备 capability 必须逐项验证。
- **直接 strict case**：heif_hardware_alpha_to_software_alpha_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 验证 alpha 图存在、YUV420/bit-depth/目标 RGBA/BGRA 约束及 sample size。
  2. 验证 alpha 图 HDI 解码、SurfaceBuffer 读写、stride 和 FillAlphaChannel 输出正确性。
  3. 硬件 alpha 解码失败时验证 SwApplyAlphaImage 的功能回退和性能影响。

### `HeifDecoderImpl::HwDecodeImage`

将 HEIF grid 或 hvc1 数据组织为 HDI 输入并请求硬件输出 SurfaceBuffer。

HeifDecoderImpl 的主图与辅助图路径都调用 HwDecodeImage；它构造 HEIF decode 参数、Ashmem 输入、NativeBuffer 输出，并在失败时调用 SwDecode 或 SwDecodeAuxiliaryImage。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp:654`；库：image framework HEIF decoder；SystemCapability：system image framework integration。
- **代码/AST 信号**：HEIF/HEIC、grid 或 hvc1；HVCC/bitstream/Ashmem 输入；NativeBuffer/SurfaceBuffer、YUV/RGBA、tile 宽高；主图、辅助图或 alpha 合成与软件回退。
- **数据形态**：Ashmem[] bitstream；NativeBuffer 输出；CodecHeifDecInfo、grid 行列/tile、像素格式与色彩空间。
- **Top-N 匹配别名**：heif_hw_decode, heifdecoderimpl_hwdecodeimage, hwdecodeimage。
- **单独卸载边界**：该 HDI 合同只证明系统图像框架存在硬件路径；格式、grid 布局、sample size、色彩空间、service/driver 和设备 capability 必须逐项验证。
- **直接 strict case**：heif_hardware_auxiliary_decode_to_software_fallback, heif_hardware_primary_decode_to_software_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 验证 HEIF 硬件解码开关、grid/hvc1 形态、tile/显示尺寸、sample size、像素格式和 SurfaceBuffer allocator。
  2. 验证 HVCC/bitstream Ashmem、NativeBuffer 输出、ICodecImage 返回码、色彩空间和 cache/fence 同步。
  3. 主图或辅助图硬件失败时验证 SwDecode/SwDecodeAuxiliaryImage 路径和等价性。

### `ICodecComponentManager::CreateComponent`

按能力返回的 component 名称创建驱动 codec 组件及回调通道。

当前 HCodec::OnAllocateComponent 直接调用该 HDI 创建 component。接口位于驱动合同，不是应用 NDK；componentName、callback、buffer ownership、状态机和销毁都必须由系统媒体栈协调。

- **暴露层级**：`driver_hdi_contract`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/drivers/interface/codec/v4_0/ICodecComponentManager.idl:121`；库：Codec Component HDI v4.0；SystemCapability：system driver integration。
- **代码/AST 信号**：HCodec/CreateComponent/ICodecComponent；输入输出 buffer 或 Surface；codec callback 与状态机；video encode/decode 生命周期。
- **数据形态**：CodecCompCapability 和 componentId；compressed bitstream 或 raw frame；SurfaceBuffer、DMA buffer、fence 与 callback。
- **Top-N 匹配别名**：codec_component_hdi, createcomponent, icodeccomponentmanager_createcomponent。
- **单独卸载边界**：组件创建与 buffer 生命周期属于系统媒体栈/HDI 集成；普通热点不能绕过 AVCodec API 直接调用它，且必须保留 FCodec/software 回退。
- **直接 strict case**：avcodec_hcodec_to_fcodec_ordered_factory_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 仅使用经当前设备 capability 验证的 component 名称创建实例，并检查 HDF 返回码和 componentId。
  2. 验证 callback、buffer/surface/fence、状态转换和 DestroyComponent 在错误路径中均可正确回收。
  3. HDI 创建或运行失败时不重试为假阳性硬件路径，转向系统 software codec 回退。

### `ICodecComponentManager::GetComponentCapabilityList`

读取驱动提供的视频编解码 component 能力列表。

HCodecList 先获取 component 数量再读取 CodecCompCapability 列表，并仅将受支持视频 role 暴露给上层。此 HDI 合同是设备能力证据的来源，但位于系统媒体栈/驱动集成边界。

- **暴露层级**：`driver_hdi_contract`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/drivers/interface/codec/v4_0/ICodecComponentManager.idl:98`；库：Codec Component HDI v4.0；SystemCapability：system driver integration。
- **代码/AST 信号**：HCodec/CodecComponentManager/Capability 语义；codec MIME、role、profile、width/height 或 pixel format；硬件 component 名称与 capability 查询；硬件优先、软件 factory 回退控制流。
- **数据形态**：CodecCompCapability；MIME/role/profile/level；宽高、对齐、帧率、bitrate、pixel format。
- **Top-N 匹配别名**：codec_component_hdi, codeccompcapability, getcomponentcapabilitylist。
- **单独卸载边界**：HCodec/HDI 能力项来自设备运行时；源码中的 component 名称、枚举接口或 HCodec factory 不能作为某设备支持某格式的证明。
- **直接 strict case**：avcodec_hcodec_to_fcodec_ordered_factory_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 在目标设备上成功取得 component 数量和 capability list，并保存 vendor/component 名称、role、profile、尺寸、对齐和 instance limit。
  2. 把 capability 记录与真实输入 MIME/profile/size/format 做交集，不得把单个 component 的存在泛化为所有视频格式可用。
  3. 能力为空、服务不可用或约束不满足时进入 FCodec/software codec 选择。

### `ICodecImage::DoHeifDecode`

将 HEIF Ashmem bitstream、NativeBuffer 输出和 CodecHeifDecInfo 提交给图像 codec 驱动服务。

Image Codec HDI v2.1 定义 HEIF 解码合同，HeifDecoderImpl 已直接调用该接口处理 grid/hvc1 场景。它是系统/驱动层 API，应用不可将其当作公开 HEIF NDK。

- **暴露层级**：`driver_hdi_contract`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/drivers/interface/codec/image/v2_1/ICodecImage.idl:213`；库：Image Codec HDI v2.1；SystemCapability：system driver integration。
- **代码/AST 信号**：HEIF/HEIC、grid 或 hvc1；HVCC/bitstream/Ashmem 输入；NativeBuffer/SurfaceBuffer、YUV/RGBA、tile 宽高；主图、辅助图或 alpha 合成与软件回退。
- **数据形态**：Ashmem[] bitstream；NativeBuffer 输出；CodecHeifDecInfo、grid 行列/tile、像素格式与色彩空间。
- **Top-N 匹配别名**：codec_image_heif, doheifdecode, icodecimage_doheifdecode。
- **单独卸载边界**：该 HDI 合同只证明系统图像框架存在硬件路径；格式、grid 布局、sample size、色彩空间、service/driver 和设备 capability 必须逐项验证。
- **直接 strict case**：heif_hardware_auxiliary_decode_to_software_fallback, heif_hardware_primary_decode_to_software_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 验证 ICodecImage 服务、HEIF driver capability、Ashmem 输入、NativeBuffer 输出和 CodecHeifDecInfo。
  2. 检查 grid/hvc1、tile、尺寸、像素格式、色彩空间、cache/fence 与 HDI 返回值。
  3. HDI/driver/格式失败时回到 HeifDecoderImpl 的软件主图或辅助图解码。

### `ICodecImage::DoJpegDecode`

将 JPEG 输入/输出 CodecImageBuffer 与解码参数提交给图像 codec 驱动服务。

Image Codec HDI v2.1 定义 JPEG 硬件解码合同；JpegHardwareDecoder::DoDecode 已直接调用该接口。它是系统/驱动边界，普通应用不能仅因 IDL 存在而直接依赖。

- **暴露层级**：`driver_hdi_contract`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/drivers/interface/codec/image/v2_1/ICodecImage.idl:131`；库：Image Codec HDI v2.1；SystemCapability：system driver integration。
- **代码/AST 信号**：JPEG/JPG 解码；压缩 bitstream 与输出 PixelMap/NativeBuffer；sample size、颜色格式、宽高/对齐或 DMA buffer；ICodecImage/DoJpegDecode 与软件解码回退。
- **数据形态**：CodecImageBuffer 输入/输出；CodecJpegDecInfo；DMA/NativeBuffer、sample size、YUV/RGBA 输出。
- **Top-N 匹配别名**：codec_image_jpeg, dojpegdecode, icodecimage_dojpegdecode。
- **单独卸载边界**：ICodecImage 是系统/驱动 HDI 合同；调用侧还必须满足实际 capability、buffer allocator、对齐、颜色格式和服务可用性，不能视为普通 NDK。
- **直接 strict case**：jpeg_hardware_decode_to_skia_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 验证 ICodecImage 服务、Init(CODEC_IMAGE_JPEG)、driver capability 和 HDF 返回值。
  2. 验证 CodecImageBuffer、CodecJpegDecInfo、Native/DMA buffer、fence、尺寸/对齐和输出像素格式。
  3. 服务不可用、格式不支持或 HDI 调用失败时转向 ExtDecoder/Skia 软件解码。

### `JpegHardwareDecoder::Decode`

准备 JPEG 输入 buffer、检查 HDI capability 并调用 Image Codec HDI。

该系统 wrapper 以 CodecImageBuffer、CodecJpegDecInfo、sample size 和输出 buffer 组织硬件解码；它展示了 bitstream 拷贝、DMA pool、颜色格式和资源回收的真实成本，不能只将 JPEG 解码当作零开销硬件调用。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/image_framework/plugins/common/libs/image/libextplugin/src/hardware/jpeg_hw_decoder.cpp:177`；库：image framework JPEG hardware decoder；SystemCapability：system image framework integration。
- **代码/AST 信号**：JPEG/JPG 解码；压缩 bitstream 与输出 PixelMap/NativeBuffer；sample size、颜色格式、宽高/对齐或 DMA buffer；ICodecImage/DoJpegDecode 与软件解码回退。
- **数据形态**：CodecImageBuffer 输入/输出；CodecJpegDecInfo；DMA/NativeBuffer、sample size、YUV/RGBA 输出。
- **Top-N 匹配别名**：codecimagebuffer, jpeg_hw_decoder, jpeghardwaredecoder。
- **单独卸载边界**：ICodecImage 是系统/驱动 HDI 合同；调用侧还必须满足实际 capability、buffer allocator、对齐、颜色格式和服务可用性，不能视为普通 NDK。
- **直接 strict case**：jpeg_hardware_decode_to_skia_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 检查 ICodecImage 获取、JPEG capability、输入颜色格式、sample size、输入/输出 CodecImageBuffer 和 native/DMA buffer 生命周期。
  2. 分别测量 bitstream header/复制、HDI decode、色彩转换与输出同步，避免把准备开销遗漏。
  3. HDI 返回失败或服务死亡后释放已分配资源并执行软件解码。

### `MediaAVCodec::VideoDecoderFactory::CreateByMime`

在缩略图等系统路径中按 MIME 创建硬件 decoder，并在回退时按软件能力名称创建 decoder。

AVThumbnailGenerator 通过 CreateByMime 走硬件 decoder，软件分支基于 SOFTWARE capability 的 codecName 调用 CreateByName。该工厂属于系统内部，但展示了正确的硬件/软件选择顺序。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/av_codec/services/media_engine/filters/video_decoder_adapter.cpp:152`；库：AVCodec media engine internal factory；SystemCapability：system media service integration。
- **代码/AST 信号**：video decoder/codec MIME；HARDWARE/SOFTWARE capability 分支；width/height、profile、surface 或 AVFormat；创建失败后 software decoder 回退。
- **数据形态**：视频 MIME/码流；width/height/profile；AVFormat、SurfaceBuffer 或 decoder callback。
- **Top-N 匹配别名**：createbymime, hardware_video_decoder, videodecoderfactory_createbymime。
- **单独卸载边界**：这是系统实现选择逻辑；业务代码应优先通过公开 AVCodec capability 与生命周期 API 获取同等语义，不应依赖 internal filter/factory 符号。
- **直接 strict case**：thumbnail_hardware_decoder_to_software_reinitialization。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 检查目标 MIME 的 hardware capability、CreateByMime、Configure/Start 和输入输出 buffer/surface。
  2. 当硬件实例化或运行失败时，按 SOFTWARE capability 返回的准确 codecName 创建软件 decoder。
  3. 针对缩略图或真实视频 workload 比较初始化开销、首帧时延、解码吞吐和回退正确性。

### `OH_AVCodec_GetCapabilityByCategory`

按 MIME、编解码方向和 HARDWARE/SOFTWARE 类别查询 codec capability。

这是公共 AVCodec 硬件选择的首要门禁。只有返回非空 capability 才能继续按名称创建；它还不能证明具体 profile、format、surface 或运行时资源可用。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcapability.h:168`；库：AVCodec NDK；SystemCapability：SystemCapability.Multimedia.Media.CodecBase。
- **代码/AST 信号**：video/codec/encode/decode 调用；MIME 类型；分辨率、profile、bitrate 或 surface；硬件/软件回退分支。
- **数据形态**：OH_AVFormat；codec capability；输入输出 frame 或 surface。
- **Top-N 匹配别名**：hardware_codec, mime, oh_avcodec_getcapabilitybycategory, software_codec。
- **单独卸载边界**：能力查询必须先于按名称创建；硬件 category 命中仍需配置、启动和实测，失败时保留 software codec。
- **直接 strict case**：webview_video_decoder_hardware_to_software_creation_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoDecoder_Configure`

以 OH_AVFormat 配置 decoder 的实际输入输出参数。

硬件可用性在 Configure/Prepare 才会受到真实 profile、level、size、pixel format 和 surface/buffer 约束；能力查询成功不免除这一步。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videodecoder.h:178`；库：video decoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoDecoder。
- **代码/AST 信号**：压缩视频码流；decode callback/surface；frame buffer 或 surface；codec state machine。
- **数据形态**：MIME/profile/width/height；OH_AVFormat；输入码流与输出 surface/buffer。
- **Top-N 匹配别名**：avformat, oh_videodecoder_configure, pixel_format, video_decode。
- **单独卸载边界**：生命周期 API 不会把任意图像循环变为硬件解码；必须存在受支持压缩码流和 capability 证据。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoDecoder_CreateByMime`

基于 MIME 创建视频解码器实例。

推荐的创建入口会由系统选择可用 decoder；若目标是明确硬件路径，应先用 capability category 查询并基于返回名称创建，同时准备 software fallback。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videodecoder.h:58`；库：video decoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoDecoder。
- **代码/AST 信号**：压缩视频码流；decode callback/surface；frame buffer 或 surface；codec state machine。
- **数据形态**：MIME/profile/width/height；OH_AVFormat；输入码流与输出 surface/buffer。
- **Top-N 匹配别名**：decode, mime, oh_videodecoder_createbymime, video_decoder。
- **单独卸载边界**：生命周期 API 不会把任意图像循环变为硬件解码；必须存在受支持压缩码流和 capability 证据。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoDecoder_CreateByName`

使用 capability 查询返回的准确名称创建视频解码器。

这是硬件/软件选择后建立 decoder 的关键入口；名称必须来自当前设备 capability，不能把源码中的 HCodec 名称硬编码为跨设备事实。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videodecoder.h:71`；库：video decoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoDecoder。
- **代码/AST 信号**：压缩视频码流；decode callback/surface；frame buffer 或 surface；codec state machine。
- **数据形态**：MIME/profile/width/height；OH_AVFormat；输入码流与输出 surface/buffer。
- **Top-N 匹配别名**：codec_name, hardware_decoder, oh_videodecoder_createbyname, video_decode。
- **单独卸载边界**：生命周期 API 不会把任意图像循环变为硬件解码；必须存在受支持压缩码流和 capability 证据。
- **直接 strict case**：webview_video_decoder_hardware_to_software_creation_fallback。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoDecoder_Destroy`

释放 decoder 内部资源与实例。

资源释放需要在同一 codec 状态机中执行；它是可实施性/稳定性条件，不是加速候选的正向证据。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videodecoder.h:85`；库：video decoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoDecoder。
- **代码/AST 信号**：压缩视频码流；decode callback/surface；frame buffer 或 surface；codec state machine。
- **数据形态**：MIME/profile/width/height；OH_AVFormat；输入码流与输出 surface/buffer。
- **Top-N 匹配别名**：codec_destroy, oh_videodecoder_destroy, video_decode。
- **单独卸载边界**：生命周期 API 不会把任意图像循环变为硬件解码；必须存在受支持压缩码流和 capability 证据。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoDecoder_Prepare`

完成 decoder 初始化并进入可启动状态。

Prepare 之后才能可靠地发现部分 runtime/service 失败；应把失败转入软件 codec 或上层降级，不能把 create 成功当作硬件稳定可用。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videodecoder.h:89`；库：video decoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoDecoder。
- **代码/AST 信号**：压缩视频码流；decode callback/surface；frame buffer 或 surface；codec state machine。
- **数据形态**：MIME/profile/width/height；OH_AVFormat；输入码流与输出 surface/buffer。
- **Top-N 匹配别名**：codec_prepare, oh_videodecoder_prepare, video_decode。
- **单独卸载边界**：生命周期 API 不会把任意图像循环变为硬件解码；必须存在受支持压缩码流和 capability 证据。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoDecoder_Start`

开始 decoder 的异步/缓冲处理。

Start 后需要通过 callback、buffer/surface 交接和实际 workload 验证吞吐与时延；不要只依据接口成功返回估计帧率收益。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videodecoder.h:211`；库：video decoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoDecoder。
- **代码/AST 信号**：压缩视频码流；decode callback/surface；frame buffer 或 surface；codec state machine。
- **数据形态**：MIME/profile/width/height；OH_AVFormat；输入码流与输出 surface/buffer。
- **Top-N 匹配别名**：codec_start, oh_videodecoder_start, video_decode。
- **单独卸载边界**：生命周期 API 不会把任意图像循环变为硬件解码；必须存在受支持压缩码流和 capability 证据。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoDecoder_Stop`

停止 decoder 并结束当前处理会话。

停止/flush 生命周期是硬件与软件 codec 切换、资源回收和失败恢复的一部分；不参与候选计算形态判断。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videodecoder.h:228`；库：video decoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoDecoder。
- **代码/AST 信号**：压缩视频码流；decode callback/surface；frame buffer 或 surface；codec state machine。
- **数据形态**：MIME/profile/width/height；OH_AVFormat；输入码流与输出 surface/buffer。
- **Top-N 匹配别名**：codec_stop, oh_videodecoder_stop, video_decode。
- **单独卸载边界**：生命周期 API 不会把任意图像循环变为硬件解码；必须存在受支持压缩码流和 capability 证据。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoEncoder_Configure`

以 OH_AVFormat 配置 encoder 的实际格式与编码参数。

硬件编码能否建立取决于输入 pixel format、size、bitrate、profile、surface/buffer 和驱动资源；Configure 失败必须回退。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videoencoder.h:208`；库：video encoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoEncoder。
- **代码/AST 信号**：raw video frame；encode callback/surface；bitrate/GOP/profile；codec state machine。
- **数据形态**：像素格式与分辨率；OH_AVFormat；输入 frame/surface 与压缩输出码流。
- **Top-N 匹配别名**：avformat, bitrate, oh_videoencoder_configure, video_encode。
- **单独卸载边界**：硬件编码需要真实支持的 profile、分辨率、format 和 surface/buffer 路径；不可仅凭图像数据循环推断。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoEncoder_CreateByMime`

基于 MIME 创建视频编码器实例。

该创建入口不承诺硬件实现；应先查询 HARDWARE category 并处理不支持的 mime/profile/分辨率，保留软件编码器回退。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videoencoder.h:72`；库：video encoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoEncoder。
- **代码/AST 信号**：raw video frame；encode callback/surface；bitrate/GOP/profile；codec state machine。
- **数据形态**：像素格式与分辨率；OH_AVFormat；输入 frame/surface 与压缩输出码流。
- **Top-N 匹配别名**：encode, mime, oh_videoencoder_createbymime, video_encoder。
- **单独卸载边界**：硬件编码需要真实支持的 profile、分辨率、format 和 surface/buffer 路径；不可仅凭图像数据循环推断。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoEncoder_CreateByName`

使用 capability 查询返回的准确名称创建视频编码器。

按名称创建是把运行时 capability 和 codec 实例连接起来的步骤；没有当前设备 capability 证据时不能假定名称对应硬件编码器。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videoencoder.h:85`；库：video encoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoEncoder。
- **代码/AST 信号**：raw video frame；encode callback/surface；bitrate/GOP/profile；codec state machine。
- **数据形态**：像素格式与分辨率；OH_AVFormat；输入 frame/surface 与压缩输出码流。
- **Top-N 匹配别名**：codec_name, hardware_encoder, oh_videoencoder_createbyname, video_encode。
- **单独卸载边界**：硬件编码需要真实支持的 profile、分辨率、format 和 surface/buffer 路径；不可仅凭图像数据循环推断。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoEncoder_Destroy`

释放 encoder 内部资源与实例。

资源销毁属于生命周期契约，必须和异常回退、surface/buffer 释放和下一次 hardware/software 创建协调。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videoencoder.h:99`；库：video encoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoEncoder。
- **代码/AST 信号**：raw video frame；encode callback/surface；bitrate/GOP/profile；codec state machine。
- **数据形态**：像素格式与分辨率；OH_AVFormat；输入 frame/surface 与压缩输出码流。
- **Top-N 匹配别名**：codec_destroy, oh_videoencoder_destroy, video_encode。
- **单独卸载边界**：硬件编码需要真实支持的 profile、分辨率、format 和 surface/buffer 路径；不可仅凭图像数据循环推断。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoEncoder_Prepare`

完成 encoder 初始化并进入可启动状态。

Prepare 是 profile/format/资源约束继续生效的阶段；需结合 callback 与真实帧输入检验实际结果。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videoencoder.h:171`；库：video encoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoEncoder。
- **代码/AST 信号**：raw video frame；encode callback/surface；bitrate/GOP/profile；codec state machine。
- **数据形态**：像素格式与分辨率；OH_AVFormat；输入 frame/surface 与压缩输出码流。
- **Top-N 匹配别名**：codec_prepare, oh_videoencoder_prepare, video_encode。
- **单独卸载边界**：硬件编码需要真实支持的 profile、分辨率、format 和 surface/buffer 路径；不可仅凭图像数据循环推断。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoEncoder_Start`

开始 encoder 的异步/缓冲处理。

只有在真实输入帧、回调、surface/buffer 交接和 drain 完整参与时，才能评价硬件编码路径的吞吐、功耗和时延。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videoencoder.h:317`；库：video encoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoEncoder。
- **代码/AST 信号**：raw video frame；encode callback/surface；bitrate/GOP/profile；codec state machine。
- **数据形态**：像素格式与分辨率；OH_AVFormat；输入 frame/surface 与压缩输出码流。
- **Top-N 匹配别名**：codec_start, oh_videoencoder_start, video_encode。
- **单独卸载边界**：硬件编码需要真实支持的 profile、分辨率、format 和 surface/buffer 路径；不可仅凭图像数据循环推断。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

### `OH_VideoEncoder_Stop`

停止 encoder 并结束当前处理会话。

停止与 drain/flush/资源回收共同决定 codec 切换是否正确；它不作为发现热点可卸载性的正向证据。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/av_codec/native_avcodec_videoencoder.h:333`；库：video encoder NDK；SystemCapability：SystemCapability.Multimedia.Media.VideoEncoder。
- **代码/AST 信号**：raw video frame；encode callback/surface；bitrate/GOP/profile；codec state machine。
- **数据形态**：像素格式与分辨率；OH_AVFormat；输入 frame/surface 与压缩输出码流。
- **Top-N 匹配别名**：codec_stop, oh_videoencoder_stop, video_encode。
- **单独卸载边界**：硬件编码需要真实支持的 profile、分辨率、format 和 surface/buffer 路径；不可仅凭图像数据循环推断。
- **直接 strict case**：无。
- **能力边界**：AVCodec 的 HARDWARE category、HCodec Component HDI 与 Image Codec HDI 都仅证明源码/合同存在；只有当前设备的 runtime capability、服务/驱动、格式与 buffer 路径验证成功后才是可用候选，且仍不证明收益。
- **运行时验证**：
  1. 先用 OH_AVCodec_GetCapabilityByCategory 以真实 MIME、编解码方向和 HARDWARE 类别查询 capability；空结果必须转向 software fallback。
  2. 按 capability 返回的名称创建 codec，并以真实 profile、level、width、height、pixel format、bitrate、surface/buffer 配置 Prepare/Start。
  3. 对 HCodec，查询 ICodecComponentManager 的 component capability，并对 component 名称、role、profile、对齐、buffer/surface、回调和实例上限建立运行时证据；失败时保持 FCodec/software 回退。
  4. 对 JPEG/HEIF Image Codec 路径，验证系统开关、ICodecImage 服务、输入格式/尺寸/采样率、Ashmem 或 DMA/NativeBuffer allocator、色彩空间与 HDI 返回值；失败时保持 Skia/软件回退。
  5. JPEG 硬件编码还必须验证 ImageFwkExtNativeSo 可加载及内部委派函数存在；当前工作区没有建立应用侧可直接调用的 JPEG 硬件编码 ABI。
  6. 处理 runtime error、service died、格式不支持和 drain/stop 生命周期；保持可回退的软件 codec。
  7. 在包含帧转换、surface/buffer 交接、队列延迟和回退的端到端 workload 上 benchmark。

## 构建与校验

```bash
python3 docs_ffrt/tools/build_accelerator_api_knowledge_bases.py
python3 docs_ffrt/tools/build_accelerator_api_knowledge_bases.py --check
```

产物 SHA-256：`d8572fecd0859afb0741b148ce5c413bf708f997e89b1c9eb0212a86b73d008b`
