# Hardware API Routing Samples

These standalone C++ snippets are intentionally small inputs for the
Hardware KB context strategy. They are not production implementations and do
not need to compile as a group. Paste one source file at a time into the
workbench's hotspot-code input, then choose `硬件知识库上下文`.

| File | Expected unit route | Expected API family |
| --- | --- | --- |
| `01_dsp_pcm_equalizer.cpp` | DSP | Audio Effect HDI / effect processing |
| `02_npu_matmul.cpp` | NPU | NNRT `OH_NN_OPS_MATMUL` |
| `03_gpu_rgba_brightness.cpp` | GPU | RGBA image-effect GPU path |
| `04_hcodec_video_decode_selection.cpp` | Media / HCodec | HCodec component capability and factory |
| `05_grayscale_sobel_hotspot.cpp` | GPU | Vulkan compute `vkCmdDispatch` candidate; CPU parallelism is also worth evaluating |

The outputs are candidates, not device-support or performance conclusions. In
particular, the HCodec sample models a system integration path rather than a
public application SDK call.

## Device smoke test

`device-run/build_dsp_pcm_equalizer_smoke.sh` builds a freestanding `aarch64`
ELF that executes the CPU fallback in `01_dsp_pcm_equalizer.cpp` on a device.
It validates the PCM gain and clamp results, and prints a PASS/FAIL line. This
is deliberately a CPU fallback test, not a DSP-offload test: exercising DSP
requires a real Audio Effect HDI/provider and its PCM/control-plane contract.

`device-run/build_gpu_rgba_brightness_gles3_smoke.sh` builds a dynamic `aarch64`
GLES3 test for `03_gpu_rgba_brightness.cpp`. It creates an offscreen EGL
context, uploads a two-pixel RGBA texture, applies the brightness and clamp
operation in a fragment shader, reads the result back, and prints the actual
GPU renderer before reporting PASS/FAIL. This is a real GPU pipeline test;
the readback is intentionally part of result verification, not the production
data path.

For a connected device, run the complete build-and-execute flow with:

```bash
HDC_TARGET=<device-serial> bash device-run/run_gpu_rgba_brightness_gles3_smoke.sh
```

The runner copies the target's EGL, GLES3, and libc shared objects only to
resolve the target ABI at link time. The executable loads the original system
and vendor graphics libraries on the device, prints the active renderer, and
removes its temporary device binary on exit.

`device-run/build_gpu_rgba_brightness_vulkan_smoke.sh` builds a dynamic `aarch64`
Vulkan compute test for the same brightness/clamp operation. It compiles
`device-run/brightness.comp` to SPIR-V with glslang, creates a Vulkan device and
compute pipeline, dispatches with `vkCmdDispatch`, reads the SSBO result back,
and compares it to the CPU reference. Run it with:

```bash
HDC_TARGET=<device-serial> bash device-run/run_gpu_rgba_brightness_vulkan_smoke.sh
```

The production workbench exposes the same pattern as the `gpu_vulkan` Build
Worker profile. The Agent supplies the C host and GLSL source; the platform's
fixed glslang compiler generates Vulkan 1.0 SPIR-V and records shader, compile,
link, deploy, and device execution as separate facts. A failed EGL/GLES smoke
test therefore does not prevent the Agent from testing this headless Vulkan
compute path.
