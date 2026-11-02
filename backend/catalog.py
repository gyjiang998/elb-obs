"""The source-discovered candidate catalog is intentionally separate from the formal KB."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import sha256_file, sha256_json


# These are discovery leads from the current OpenHarmony checkout.  A target is
# deliberately phrased as a hypothesis: none of these rows upgrades a hardware
# capability, cost model, or formal KB record by itself.
CASE_SEEDS: list[tuple[str, str, str, str, int, str]] = [
    ("image_blur_hpae", "image_blur", "GPU_OR_AAE", "graphic_graphic_2d/rosen/modules/render_service_base/src/hpae_base/rs_hpae_filter_cache_manager.cpp", 360, "HPAE blur cache path"),
    ("image_blur_hpae_fallback", "image_blur", "GPU_OR_AAE", "graphic_graphic_2d/rosen/modules/render_service_base/src/hpae_base/rs_hpae_filter_cache_manager.cpp", 411, "HPAE filter fallback path"),
    ("image_resize_detail", "image_resize", "AAE_ARSR_OR_GPU", "graphic_graphic_2d/rosen/modules/render_service_base/src/feature/image_detail_enhancer/rs_image_detail_enhancer_thread.cpp", 237, "detail enhancer resize path"),
    ("jpeg_hw_decode", "jpeg_decode", "JPEG_HW_DECODER", "multimedia_image_framework/plugins/common/libs/image/libextplugin/src/ext_decoder.cpp", 1619, "JPEG hardware decode dispatch"),
    ("jpeg_hw_encode", "jpeg_encode", "JPEG_HW_ENCODER", "multimedia_image_framework/plugins/common/libs/image/libextplugin/src/ext_encoder.cpp", 1842, "JPEG encoder path"),
    ("heif_hw_decode", "heif_decode", "IMAGE_HW_DECODER", "multimedia_image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp", 502, "HEIF decoder path"),
    ("heif_alpha_apply", "image_alpha_apply", "IMAGE_ACCELERATOR", "multimedia_image_framework/plugins/common/libs/image/libextplugin/src/heif_impl/HeifDecoderImpl.cpp", 518, "HEIF alpha processing"),
    ("pixelmap_gl_shader", "pixel_map_postprocess", "GPU_SHADER", "multimedia_image_framework/frameworks/innerkitsimpl/egl_image/src/pixel_map_gl_shader.cpp", 1, "PixelMap GL shader"),
    ("pixelmap_gl_program", "pixel_map_postprocess", "GPU_SHADER", "multimedia_image_framework/frameworks/innerkitsimpl/egl_image/src/pixel_map_gl_post_proc_program.cpp", 1, "PixelMap GL post-processing program"),
    ("pixelmap_gl_context", "pixel_map_postprocess", "GPU_SHADER", "multimedia_image_framework/frameworks/innerkitsimpl/egl_image/src/pixel_map_gl_context.cpp", 1, "PixelMap GL context"),
    ("pixelmap_surface_bridge", "image_surface_interop", "GPU_TEXTURE", "multimedia_image_framework/frameworks/innerkitsimpl/egl_image/src/pixel_map_from_surface.cpp", 1, "surface to PixelMap bridge"),
    ("webp_decode", "webp_decode", "IMAGE_CODEC_ACCELERATOR", "multimedia_image_framework/plugins/common/libs/image/formatagentplugin/src/webp_format_agent.cpp", 1, "WebP decoder dispatch"),
    ("webp_encode", "webp_encode", "IMAGE_CODEC_ACCELERATOR", "multimedia_image_framework/plugins/common/libs/image/libextplugin/src/ext_encoder.cpp", 1, "WebP encoder dispatch"),
    ("png_decode", "png_decode", "IMAGE_CODEC_ACCELERATOR", "multimedia_image_framework/plugins/common/libs/image/formatagentplugin/src/png_format_agent.cpp", 1, "PNG format agent"),
    ("webp_format_probe", "image_format_probe", "CPU_SIMD", "multimedia_image_framework/plugins/common/libs/image/formatagentplugin/src/webp_format_agent.cpp", 1, "WebP format probe"),
    ("svg_rasterize", "svg_rasterize", "GPU_SHADER", "multimedia_image_framework/plugins/common/libs/image/libsvgplugin/src/svg_decoder.cpp", 413, "SVG rasterization path"),
    ("raw_decode", "raw_decode", "IMAGE_CODEC_ACCELERATOR", "multimedia_image_framework/plugins/common/libs/image/formatagentplugin/src/raw_format_agent.cpp", 1, "RAW decoder dispatch"),
    ("raw_stream_copy", "image_stream_copy", "CPU_SIMD", "multimedia_image_framework/plugins/common/libs/image/librawplugin/src/raw_stream.cpp", 56, "RAW stream copy"),
    ("nn_model_compile", "nn_model_compile", "NPU", "ai_neural_network_runtime/frameworks/native/neural_network_runtime/inner_model.cpp", 1, "NN model compile path"),
    ("nn_inference_sync", "nn_inference", "NPU", "ai_neural_network_runtime/frameworks/native/neural_network_core/executor.h", 48, "NN synchronous execution API"),
    ("nn_inference_async", "nn_inference", "NPU", "ai_neural_network_runtime/frameworks/native/neural_network_core/executor.h", 52, "NN asynchronous execution API"),
    ("nn_model_schedule", "nn_model_schedule", "NPU", "ai_neural_network_runtime/frameworks/native/neural_network_core/nnrt_client.cpp", 64, "NN runtime client scheduling"),
    ("nn_tensor_prepare", "tensor_prepare", "NPU", "ai_neural_network_runtime/frameworks/native/neural_network_runtime/nn_tensor.cpp", 1, "NN tensor setup"),
    ("vulkan_task_wait", "gpu_task_synchronization", "GPU_VULKAN", "graphic_graphic_2d/rosen/modules/render_service_base/src/hpae_base/rs_hpae_ffrt_pattern_manager.cpp", 189, "Vulkan task wait"),
    ("vulkan_task_notify", "gpu_task_synchronization", "GPU_VULKAN", "graphic_graphic_2d/rosen/modules/render_service_base/src/hpae_base/rs_hpae_ffrt_pattern_manager.cpp", 205, "Vulkan task notification"),
    ("vulkan_backend_init", "graphics_backend_init", "GPU_VULKAN", "graphic_graphic_2d/rosen/modules/render_service/composer/composer_service/external_depend/engine/rs_base_render_engine.cpp", 88, "render engine Vulkan initialization"),
    ("egl_share_context", "graphics_context_create", "GPU_EGL", "graphic_graphic_2d/rosen/modules/render_service_base/src/common/rs_background_thread.cpp", 111, "background EGL context setup"),
    ("surface_self_draw", "surface_composition", "GPU_COMPOSITOR", "graphic_graphic_2d/rosen/modules/render_service/core/drawable/rs_surface_render_node_drawable.cpp", 1298, "surface composition draw path"),
    ("render_frame", "frame_render", "GPU_COMPOSITOR", "graphic_graphic_2d/rosen/modules/render_service/core/pipeline/render_thread/rs_uni_render_thread.cpp", 1, "uni render thread"),
    ("filter_effect", "image_filter", "GPU_SHADER", "graphic_graphic_2d/interfaces/kits/napi/graphic/ui_effect/src/filter_napi.cpp", 1, "UI filter effect API"),
]


# CASE_SEEDS originated from the compact source_code mirror.  The workbench
# now defaults to the full ``oh`` workspace, where these projects live below
# the canonical OpenHarmony tree.
CANONICAL_REPOSITORY_ROOTS = {
    "graphic_graphic_2d": Path("openharmony/foundation/graphic/graphic_2d"),
    "multimedia_image_framework": Path("openharmony/foundation/multimedia/image_framework"),
    "ai_neural_network_runtime": Path("openharmony/foundation/ai/neural_network_runtime"),
}


def _canonical_case_path(legacy_relative_path: str) -> Path:
    legacy_path = Path(legacy_relative_path)
    if not legacy_path.parts:
        return legacy_path
    canonical_root = CANONICAL_REPOSITORY_ROOTS.get(legacy_path.parts[0])
    if canonical_root is None:
        return legacy_path
    return canonical_root.joinpath(*legacy_path.parts[1:])


def build_catalog(workspace_root: Path) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for index, (slug, algorithm, target, relative_path, line, label) in enumerate(CASE_SEEDS, start=1):
        canonical_path = _canonical_case_path(relative_path)
        source = workspace_root / canonical_path
        evidence: dict[str, Any] = {"path": canonical_path.as_posix(), "line": line}
        missing_evidence = [
            "target hardware capability and device constraints",
            "revision-pinned workload benchmark including transfer and synchronization cost",
            "correctness, fallback, and failure-path validation",
        ]
        if source.is_file():
            evidence["sha256"] = sha256_file(source)
        else:
            evidence["sha256"] = None
            missing_evidence.insert(0, "source file is unavailable in this checkout")
        cases.append(
            {
                "case_id": f"case_{slug}_{index:03d}",
                "schema_version": "1.0",
                "lifecycle": "candidate",
                "case_kind": "source_discovered",
                "algorithm_hypothesis": algorithm,
                "target_hypothesis": target,
                "title": label,
                "source_evidence": [evidence],
                "assumptions": [
                    "The source location is a discovery lead, not a measured hotspot.",
                    "The target hypothesis is not a confirmed hardware availability claim.",
                ],
                "missing_evidence": missing_evidence,
                "promotion_links": [],
                "provenance": {"origin": "openharmony_workspace", "confidence": "candidate"},
            }
        )
    catalog = {
        "artifact_type": "knowledge_case_catalog",
        "schema_version": "1.0",
        "catalog_id": "openharmony_workspace_source_discovery_30",
        "catalog_version": "0.1.1",
        "producer": {"name": "case-catalog-builder", "version": "1.1.0"},
        "case_count": len(cases),
        "cases": cases,
    }
    catalog["content_sha256"] = sha256_json(catalog)
    return catalog


def ensure_catalog(path: Path, workspace_root: Path) -> dict[str, Any]:
    """Load the packaged candidate catalog without mutating the Git checkout."""

    if path.is_file():
        try:
            import json

            existing = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(existing, dict)
                and existing.get("artifact_type") == "knowledge_case_catalog"
                and isinstance(existing.get("cases"), list)
                and existing.get("case_count") == len(existing["cases"])
            ):
                return existing
        except Exception:
            pass
    # Development fallback only. Production packages are expected to contain
    # the versioned data file and may mount the application tree read-only.
    return build_catalog(workspace_root)
