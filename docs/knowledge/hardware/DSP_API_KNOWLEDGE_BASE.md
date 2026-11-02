# OpenHarmony DSP / Audio Effect API 知识库

> 自动生成：`python3 docs_ffrt/tools/build_accelerator_api_knowledge_bases.py`。不要手工编辑生成文件。

## 定位与边界

Audio Effect HDI/VDI 控制与处理契约、系统音频效果链回退路径，以及少量高层效果模式入口。HDI/VDI 记录不是普通应用侧 DSP SDK。

所有记录的目标设备可用性均为 `evidence_required`。接口/源码路径存在不等于设备驱动、格式、权限、实现或收益已经可用。

## 内容统计

- API/集成入口：9
- 同一硬件单元的严格 fallback 案例：1
- 与具体入口直接关联 strict case 的记录：4

## 运行时验证协议

1. 确认目标设备实际加载相应 Audio Effect HDI provider，并成功创建 effect controller；HDI/VDI 仅在对应系统/驱动权限范围内使用。
2. 以真实 sample rate、channel、frameCount、sample format、effect 参数和设备路由执行 EffectProcess/SendCommand，检查返回值和 bypass 行为。
3. 在同一实时音频 workload 上比较 DSP/HDI 与 AP effect chain 的端到端延迟、XRUN、功耗和回退正确性。

## API 目录

| API / 集成入口 | 语义族 | 暴露层级 | 一句话摘要 | 直接 strict case |
|---|---|---|---|---|
| `AudioEffectChain::ApplyEffectChain` | AP 音频效果回退链 | system_source_integration | 在 AP 上按 runtime-selected effect handle 执行音频效果链。 | audio_effect_dsp_hdi_to_ap_runtime_chain_fallback |
| `AudioEffectHdiParam::SetHdiCommand` | DSP 音频效果控制 | system_source_integration | 将系统 Audio Effect 参数写入 HDI controller 的 SendCommand。 | audio_effect_dsp_hdi_to_ap_runtime_chain_fallback |
| `EffectFactory::CreateController` | DSP 音频效果控制 | driver_hdi_contract | 由 Audio Effect factory 创建一个 effect controller。 | 无 |
| `EffectFactory::DestroyController` | DSP 音频效果控制 | driver_hdi_contract | 释放由 Audio Effect factory 创建的 controller。 | 无 |
| `IEffectControlVdi::EffectProcess` | DSP 音频效果处理 | driver_hdi_contract | 通过 HDI effect controller 对输入 PCM buffer 执行 effect processing。 | audio_effect_dsp_hdi_to_ap_runtime_chain_fallback |
| `IEffectControlVdi::EffectReverse` | DSP 音频效果处理 | driver_hdi_contract | 通过 HDI effect controller 执行 effect reverse 数据处理。 | 无 |
| `IEffectControlVdi::GetEffectDescriptor` | DSP 音频效果控制 | driver_hdi_contract | 读取 effect controller 的 effectId、库名和供应商描述。 | 无 |
| `IEffectControlVdi::SendCommand` | DSP 音频效果控制 | driver_hdi_contract | 向 effect controller 发送配置、启停、参数或查询命令。 | audio_effect_dsp_hdi_to_ap_runtime_chain_fallback |
| `OH_AVPlayer_SetAudioEffectMode` | DSP 音频效果控制 | openharmony_ndk | 为 AVPlayer 选择高层音效模式。 | 无 |

## 详细记录

### `AudioEffectChain::ApplyEffectChain`

在 AP 上按 runtime-selected effect handle 执行音频效果链。

这是 DSP/HDI 不可用时的系统回退分发路径。源码事实确认 buffer、frameLen 和 process callback，但具体 CPU 算法由运行时 provider 决定，不能直接作为函数级 AST 样本。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/audio_framework/frameworks/native/audioeffect/src/audio_effect_chain.cpp:361`；库：audio framework internal integration；SystemCapability：system audio service integration。
- **代码/AST 信号**：AudioEffectChain；AudioEffectHandle::process；PCM buffer；runtime provider/dispatcher。
- **数据形态**：float PCM buffer；frame length；effect process info。
- **Top-N 匹配别名**：applyeffectchain, audioeffectchain, effect_handle, pcm。
- **单独卸载边界**：该路径只证明分发与回退语义；必须继续解析 runtime provider 后才可得到可匹配的 CPU 计算核心。
- **直接 strict case**：audio_effect_dsp_hdi_to_ap_runtime_chain_fallback。
- **能力边界**：Audio Effect HDI 头文件和 DSP 命名 provider 只证明系统集成契约；不证明某部手机启用了 DSP offload、支持某个 effect 或能满足实时延迟。
- **运行时验证**：
  1. 确认目标设备实际加载相应 Audio Effect HDI provider，并成功创建 effect controller；HDI/VDI 仅在对应系统/驱动权限范围内使用。
  2. 以真实 sample rate、channel、frameCount、sample format、effect 参数和设备路由执行 EffectProcess/SendCommand，检查返回值和 bypass 行为。
  3. 在同一实时音频 workload 上比较 DSP/HDI 与 AP effect chain 的端到端延迟、XRUN、功耗和回退正确性。

### `AudioEffectHdiParam::SetHdiCommand`

将系统 Audio Effect 参数写入 HDI controller 的 SendCommand。

该源码入口是当前 strict DSP pair 的明确 offload 控制路径：复制 effect 参数、构造 reply 长度并发送 HDI_SET_PATAM。它是系统实现，不是应用侧公开 DSP API。

- **暴露层级**：`system_source_integration`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/foundation/multimedia/audio_framework/frameworks/native/audioeffect/src/audio_effect_hdi_param.cpp:87`；库：audio framework internal integration；SystemCapability：system audio service integration。
- **代码/AST 信号**：audio effect/equalizer/reverb/spatial 语义；设备类型与状态更新；命令 buffer 与 reply；provider/controller 生命周期。
- **数据形态**：effectId；命令字节 buffer；设备路由与 effect 配置。
- **Top-N 匹配别名**：audioeffecthdiparam, dsp_effect, sendcommand, sethdicommand。
- **单独卸载边界**：控制面命令不是数据面处理热点；只有实际 PCM effect processing 和实时约束明确时才评估 DSP 卸载。
- **直接 strict case**：audio_effect_dsp_hdi_to_ap_runtime_chain_fallback。
- **能力边界**：Audio Effect HDI 头文件和 DSP 命名 provider 只证明系统集成契约；不证明某部手机启用了 DSP offload、支持某个 effect 或能满足实时延迟。
- **运行时验证**：
  1. 确认目标设备实际加载相应 Audio Effect HDI provider，并成功创建 effect controller；HDI/VDI 仅在对应系统/驱动权限范围内使用。
  2. 以真实 sample rate、channel、frameCount、sample format、effect 参数和设备路由执行 EffectProcess/SendCommand，检查返回值和 bypass 行为。
  3. 在同一实时音频 workload 上比较 DSP/HDI 与 AP effect chain 的端到端延迟、XRUN、功耗和回退正确性。

### `EffectFactory::CreateController`

由 Audio Effect factory 创建一个 effect controller。

controller 创建必须与 effect 信息和 provider 匹配；它是 HDI 生命周期证据，不是把 CPU 音频循环自动迁移到 DSP 的操作。

- **暴露层级**：`driver_hdi_contract`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/drivers/peripheral/audio/interfaces/effect/v1_0/effect_factory.h:62`；库：audio effect VDI/HDI provider；SystemCapability：system driver integration。
- **代码/AST 信号**：audio effect/equalizer/reverb/spatial 语义；设备类型与状态更新；命令 buffer 与 reply；provider/controller 生命周期。
- **数据形态**：effectId；命令字节 buffer；设备路由与 effect 配置。
- **Top-N 匹配别名**：audio_effect, createcontroller, effect_factory, hdi_provider。
- **单独卸载边界**：控制面命令不是数据面处理热点；只有实际 PCM effect processing 和实时约束明确时才评估 DSP 卸载。
- **直接 strict case**：无。
- **能力边界**：Audio Effect HDI 头文件和 DSP 命名 provider 只证明系统集成契约；不证明某部手机启用了 DSP offload、支持某个 effect 或能满足实时延迟。
- **运行时验证**：
  1. 确认目标设备实际加载相应 Audio Effect HDI provider，并成功创建 effect controller；HDI/VDI 仅在对应系统/驱动权限范围内使用。
  2. 以真实 sample rate、channel、frameCount、sample format、effect 参数和设备路由执行 EffectProcess/SendCommand，检查返回值和 bypass 行为。
  3. 在同一实时音频 workload 上比较 DSP/HDI 与 AP effect chain 的端到端延迟、XRUN、功耗和回退正确性。

### `EffectFactory::DestroyController`

释放由 Audio Effect factory 创建的 controller。

controller 生命周期必须与实时音频流停止、参数释放和 fallback 切换协调；它本身不代表 DSP 数据面优化。

- **暴露层级**：`driver_hdi_contract`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/drivers/peripheral/audio/interfaces/effect/v1_0/effect_factory.h:76`；库：audio effect VDI/HDI provider；SystemCapability：system driver integration。
- **代码/AST 信号**：audio effect/equalizer/reverb/spatial 语义；设备类型与状态更新；命令 buffer 与 reply；provider/controller 生命周期。
- **数据形态**：effectId；命令字节 buffer；设备路由与 effect 配置。
- **Top-N 匹配别名**：audio_effect, destroycontroller, effect_factory, hdi_provider。
- **单独卸载边界**：控制面命令不是数据面处理热点；只有实际 PCM effect processing 和实时约束明确时才评估 DSP 卸载。
- **直接 strict case**：无。
- **能力边界**：Audio Effect HDI 头文件和 DSP 命名 provider 只证明系统集成契约；不证明某部手机启用了 DSP offload、支持某个 effect 或能满足实时延迟。
- **运行时验证**：
  1. 确认目标设备实际加载相应 Audio Effect HDI provider，并成功创建 effect controller；HDI/VDI 仅在对应系统/驱动权限范围内使用。
  2. 以真实 sample rate、channel、frameCount、sample format、effect 参数和设备路由执行 EffectProcess/SendCommand，检查返回值和 bypass 行为。
  3. 在同一实时音频 workload 上比较 DSP/HDI 与 AP effect chain 的端到端延迟、XRUN、功耗和回退正确性。

### `IEffectControlVdi::EffectProcess`

通过 HDI effect controller 对输入 PCM buffer 执行 effect processing。

这是 DSP/HDI 数据面接口：输入输出均为带 frameCount、datatag、rawData 的 AudioEffectBufferVdi。它处在驱动/系统集成层，普通业务代码不可仅凭该符号直接调用。

- **暴露层级**：`driver_hdi_contract`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/drivers/peripheral/audio/interfaces/effect/v1_0/ieffect_control_vdi.h:30`；库：audio effect VDI/HDI provider；SystemCapability：system driver integration。
- **代码/AST 信号**：PCM/sample/frame buffer；按帧或按采样遍历；效果参数、滤波、混音或空间化语义；输入输出音频 buffer。
- **数据形态**：frameCount；S16/S32/F32 等 sample format；interleaved 或按 provider 定义的 buffer。
- **Top-N 匹配别名**：audio_effect, effectprocess, framecount, pcm。
- **单独卸载边界**：必须确认具体 effect provider、采样率/声道/格式、实时延迟与 bypass/fallback；AP effect chain 不是具体 DSP 算法证明。
- **直接 strict case**：audio_effect_dsp_hdi_to_ap_runtime_chain_fallback。
- **能力边界**：Audio Effect HDI 头文件和 DSP 命名 provider 只证明系统集成契约；不证明某部手机启用了 DSP offload、支持某个 effect 或能满足实时延迟。
- **运行时验证**：
  1. 确认目标设备实际加载相应 Audio Effect HDI provider，并成功创建 effect controller；HDI/VDI 仅在对应系统/驱动权限范围内使用。
  2. 以真实 sample rate、channel、frameCount、sample format、effect 参数和设备路由执行 EffectProcess/SendCommand，检查返回值和 bypass 行为。
  3. 在同一实时音频 workload 上比较 DSP/HDI 与 AP effect chain 的端到端延迟、XRUN、功耗和回退正确性。

### `IEffectControlVdi::EffectReverse`

通过 HDI effect controller 执行 effect reverse 数据处理。

该接口同样以 AudioEffectBufferVdi 传递按帧音频数据；是否有 DSP provider、reverse 语义和格式支持需在实际 HDI 上验证。

- **暴露层级**：`driver_hdi_contract`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/drivers/peripheral/audio/interfaces/effect/v1_0/ieffect_control_vdi.h:35`；库：audio effect VDI/HDI provider；SystemCapability：system driver integration。
- **代码/AST 信号**：PCM/sample/frame buffer；按帧或按采样遍历；效果参数、滤波、混音或空间化语义；输入输出音频 buffer。
- **数据形态**：frameCount；S16/S32/F32 等 sample format；interleaved 或按 provider 定义的 buffer。
- **Top-N 匹配别名**：audio_effect, effectreverse, pcm, reverse_effect。
- **单独卸载边界**：必须确认具体 effect provider、采样率/声道/格式、实时延迟与 bypass/fallback；AP effect chain 不是具体 DSP 算法证明。
- **直接 strict case**：无。
- **能力边界**：Audio Effect HDI 头文件和 DSP 命名 provider 只证明系统集成契约；不证明某部手机启用了 DSP offload、支持某个 effect 或能满足实时延迟。
- **运行时验证**：
  1. 确认目标设备实际加载相应 Audio Effect HDI provider，并成功创建 effect controller；HDI/VDI 仅在对应系统/驱动权限范围内使用。
  2. 以真实 sample rate、channel、frameCount、sample format、effect 参数和设备路由执行 EffectProcess/SendCommand，检查返回值和 bypass 行为。
  3. 在同一实时音频 workload 上比较 DSP/HDI 与 AP effect chain 的端到端延迟、XRUN、功耗和回退正确性。

### `IEffectControlVdi::GetEffectDescriptor`

读取 effect controller 的 effectId、库名和供应商描述。

可用于确认 selected effect provider 的身份；它不等价于 DSP capability、算法类型或数据面吞吐。

- **暴露层级**：`driver_hdi_contract`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/drivers/peripheral/audio/interfaces/effect/v1_0/ieffect_control_vdi.h:34`；库：audio effect VDI/HDI provider；SystemCapability：system driver integration。
- **代码/AST 信号**：audio effect/equalizer/reverb/spatial 语义；设备类型与状态更新；命令 buffer 与 reply；provider/controller 生命周期。
- **数据形态**：effectId；命令字节 buffer；设备路由与 effect 配置。
- **Top-N 匹配别名**：audio_effect, effectid, geteffectdescriptor, provider。
- **单独卸载边界**：控制面命令不是数据面处理热点；只有实际 PCM effect processing 和实时约束明确时才评估 DSP 卸载。
- **直接 strict case**：无。
- **能力边界**：Audio Effect HDI 头文件和 DSP 命名 provider 只证明系统集成契约；不证明某部手机启用了 DSP offload、支持某个 effect 或能满足实时延迟。
- **运行时验证**：
  1. 确认目标设备实际加载相应 Audio Effect HDI provider，并成功创建 effect controller；HDI/VDI 仅在对应系统/驱动权限范围内使用。
  2. 以真实 sample rate、channel、frameCount、sample format、effect 参数和设备路由执行 EffectProcess/SendCommand，检查返回值和 bypass 行为。
  3. 在同一实时音频 workload 上比较 DSP/HDI 与 AP effect chain 的端到端延迟、XRUN、功耗和回退正确性。

### `IEffectControlVdi::SendCommand`

向 effect controller 发送配置、启停、参数或查询命令。

它是控制面接口：命令 buffer 和 reply buffer 需要匹配 provider 约定。控制面成功并不说明数据面 EffectProcess 已在 DSP 执行或具有性能收益。

- **暴露层级**：`driver_hdi_contract`；应用侧直接使用：`not_established_or_system_integration_only`。
- **源码契约**：`openharmony/drivers/peripheral/audio/interfaces/effect/v1_0/ieffect_control_vdi.h:32`；库：audio effect VDI/HDI provider；SystemCapability：system driver integration。
- **代码/AST 信号**：audio effect/equalizer/reverb/spatial 语义；设备类型与状态更新；命令 buffer 与 reply；provider/controller 生命周期。
- **数据形态**：effectId；命令字节 buffer；设备路由与 effect 配置。
- **Top-N 匹配别名**：audio_effect, effect_param, hdi_command, sendcommand。
- **单独卸载边界**：控制面命令不是数据面处理热点；只有实际 PCM effect processing 和实时约束明确时才评估 DSP 卸载。
- **直接 strict case**：audio_effect_dsp_hdi_to_ap_runtime_chain_fallback。
- **能力边界**：Audio Effect HDI 头文件和 DSP 命名 provider 只证明系统集成契约；不证明某部手机启用了 DSP offload、支持某个 effect 或能满足实时延迟。
- **运行时验证**：
  1. 确认目标设备实际加载相应 Audio Effect HDI provider，并成功创建 effect controller；HDI/VDI 仅在对应系统/驱动权限范围内使用。
  2. 以真实 sample rate、channel、frameCount、sample format、effect 参数和设备路由执行 EffectProcess/SendCommand，检查返回值和 bypass 行为。
  3. 在同一实时音频 workload 上比较 DSP/HDI 与 AP effect chain 的端到端延迟、XRUN、功耗和回退正确性。

### `OH_AVPlayer_SetAudioEffectMode`

为 AVPlayer 选择高层音效模式。

这是公开应用 API 的模式选择入口，而不是通用 DSP kernel/HDI 控制接口。实际是否走硬件、哪一个 effect provider 和数据格式均由系统运行时决定。

- **暴露层级**：`openharmony_ndk`；应用侧直接使用：`requires_sdk_and_device_validation`。
- **源码契约**：`openharmony/interface/sdk_c/multimedia/player_framework/avplayer.h:369`；库：AVPlayer NDK；SystemCapability：SystemCapability.Multimedia.Media.Player。
- **代码/AST 信号**：audio effect/equalizer/reverb/spatial 语义；设备类型与状态更新；命令 buffer 与 reply；provider/controller 生命周期。
- **数据形态**：effectId；命令字节 buffer；设备路由与 effect 配置。
- **Top-N 匹配别名**：audio_effect_mode, avplayer, setaudioeffectmode, spatial_audio。
- **单独卸载边界**：控制面命令不是数据面处理热点；只有实际 PCM effect processing 和实时约束明确时才评估 DSP 卸载。
- **直接 strict case**：无。
- **能力边界**：Audio Effect HDI 头文件和 DSP 命名 provider 只证明系统集成契约；不证明某部手机启用了 DSP offload、支持某个 effect 或能满足实时延迟。
- **运行时验证**：
  1. 确认目标设备实际加载相应 Audio Effect HDI provider，并成功创建 effect controller；HDI/VDI 仅在对应系统/驱动权限范围内使用。
  2. 以真实 sample rate、channel、frameCount、sample format、effect 参数和设备路由执行 EffectProcess/SendCommand，检查返回值和 bypass 行为。
  3. 在同一实时音频 workload 上比较 DSP/HDI 与 AP effect chain 的端到端延迟、XRUN、功耗和回退正确性。

## 构建与校验

```bash
python3 docs_ffrt/tools/build_accelerator_api_knowledge_bases.py
python3 docs_ffrt/tools/build_accelerator_api_knowledge_bases.py --check
```

产物 SHA-256：`f0907f3e0a9aa02c85d7b73a90229ea1b803d8b42eecb1044ebe530263eaeb06`
