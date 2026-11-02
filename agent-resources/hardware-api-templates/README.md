# Hardware API Template Library

This directory contains reviewed implementation templates for the device-validation Agent. It is intentionally different from `demo/`, evaluation fixtures, generated artifacts, and historical Run workspaces.

## Current Coverage

| Template | Hardware/API | Verification level |
| --- | --- | --- |
| `gpu.vulkan.compute-buffer.v1` | Vulkan compute and `vkCmdDispatch` | Device function verified |
| `gpu.gles3.fragment-image.v1` | EGL/GLES3 fragment image pipeline | Device function verified |
| `gpu.gles31.compute-ssbo.v1` | EGL/GLES 3.1 compute and SSBO | Device function verified |
| `npu.nnrt.graph-lifecycle.v1` | OpenHarmony NNRT graph lifecycle plus operator adapter guide | Device negative observation for the sample MatMul |
| `media.avcodec.hardware-capability.v1` | AVCodec hardware-category capability | Source contract verified |
| `media.avcodec.lifecycle.recipe.v1` | Public AVCodec encoder/decoder lifecycle | Adapter recipe |
| `media.hcodec.component.recipe.v1` | HCodec Component HDI lifecycle | System integration recipe |
| `media.image-codec.recipe.v1` | JPEG/HEIF Image Codec HDI lifecycle | System integration recipe |
| `dsp.audio-effect-provider.recipe.v1` | Audio Effect HDI/provider integration | Integration recipe |

`catalog.json` is the machine contract. Every production asset has an independent version and SHA-256. At runtime, only registered files whose hashes match are copied into the Kimi Skill workspace.

Coverage is defined at API semantic-family granularity, not with one duplicated source file per symbol. All 204 current GPU, NPU, DSP and media API records must map to one of three levels:

- `executable_harness`: stable host scaffolding can be adapted and rebuilt.
- `lifecycle_with_adapter`: lifecycle is shared, but the selected operator, tensor, codec format or fixture still needs a task-specific adapter.
- `integration_recipe`: the API is a system/HDI/provider contract and cannot be presented as an application-direct executable.

Repository tests compare the catalog against all four API knowledge-base files. A newly added API semantic family without a mapping fails the coverage test.

## Reuse Contract

- Select by hardware unit, API family, exposure layer, profile, and visible data semantics, not by function-name similarity.
- Reuse fragile API scaffolding such as loader, context, graph/codec lifecycle, error handling, cleanup, and result protocol.
- Rewrite the hotspot operator or graph, data contract, deterministic inputs, CPU/software oracle, comparison rule, and benchmark.
- Re-run profile discovery, build, deployment, device execution, and functional comparison. Historical verification does not transfer to the new task.
- Report `template_id`, version, file hashes, reused sections, and rewritten sections.

For GPU experiments, select by semantics and total validation cost. Image/texture transforms normally start with the GLES fragment harness; regular buffer compute can start with the GLES 3.1 SSBO harness; Vulkan is preferred when explicit memory ownership, synchronization, general compute contracts, or an existing Vulkan pipeline matter. This is an experiment-ordering heuristic, not a performance conclusion.

## Promotion

A Demo or successful Run becomes a production template only after review, removal of task-private data, declaration of semantic match and adaptation points, evidence calibration, per-file hashing, and registration in `catalog.json`. Template-blind holdout evaluation deliberately omits this directory to measure no-template generalization separately.
