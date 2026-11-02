"""Deterministic first-stage routing for Hardware-KB reasoning.

The Hardware-KB strategy deliberately does not ask an LLM to inspect every
accelerator API record.  It first uses observable code shape plus the concise
hardware-unit descriptions in the versioned hardware KB to choose the most
plausible unit family.  Only that family's API catalog is queried afterwards.

This is a routing aid, not a device capability probe or an AST result.  A
selected unit still needs API-level semantic comparison and each record's
runtime verification gates.
"""

from __future__ import annotations

import re
from typing import Any

from .accelerator_api_retrieval import static_accelerator_observations
from .npu_api_retrieval import static_semantic_observations


ROUTABLE_UNITS = frozenset({"npu", "gpu", "dsp", "media"})
_MIN_ROUTE_SCORE = 18
_GENERIC_DESCRIPTION_TERMS = frozenset(
    {
        "accelerator",
        "api",
        "audio",
        "capability",
        "codec",
        "component",
        "compute",
        "device",
        "encoder",
        "for",
        "hardware",
        "image",
        "interface",
        "media",
        "openharmony",
        "path",
        "processing",
        "runtime",
        "software",
        "unit",
        "video",
    }
)


def _canonical_unit(record: dict[str, Any]) -> str | None:
    """Map a hardware-KB unit description to one API catalog family."""

    unit_type = str(record.get("unit_type") or "").casefold()
    hardware_id = str(record.get("hardware_id") or record.get("record_id") or "").casefold()
    combined = f"{unit_type} {hardware_id}"
    if "npu" in combined or "neural" in combined or "hiai" in combined:
        return "npu"
    if "gpu" in combined or "opengl" in combined or "opencl" in combined or "vulkan" in combined:
        return "gpu"
    if "dsp" in combined or "audio_dsp" in combined:
        return "dsp"
    if "image_codec" in combined or "video_codec" in combined or "hcodec" in combined or "jpeg" in combined or "heif" in combined:
        return "media"
    return None


def _tokens_from_text(value: str) -> set[str]:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value).casefold()
    raw = re.findall(r"[a-z][a-z0-9_]{1,}", normalized)
    parts = [part for token in raw for part in token.split("_") if len(part) >= 2]
    return set(raw) | set(parts)


def _record_description_tokens(record: dict[str, Any]) -> set[str]:
    values: list[str] = []
    for field in ("hardware_id", "record_id", "name", "unit_type", "software_interface"):
        value = record.get(field)
        if value is not None:
            values.append(str(value))
    # Constraints often contain long natural-language boilerplate such as
    # "for the target device". They remain in the selected record for human
    # review, but only stable unit/interface/algorithm names may drive route
    # matching.
    for field in ("supported_algorithms", "implementation_paths"):
        value = record.get(field)
        if isinstance(value, list):
            values.extend(str(item) for item in value if item is not None)
    return _tokens_from_text(" ".join(values))


def _public_observations(observations: dict[str, Any]) -> dict[str, Any]:
    """Keep routing diagnostics bounded; raw token lists stay out of prompts."""

    return {key: value for key, value in observations.items() if key != "_tokens"}


def _code_observations(code_unit: dict[str, Any]) -> tuple[set[str], dict[str, Any], dict[str, Any]]:
    npu = static_semantic_observations(code_unit)
    accelerator = static_accelerator_observations(code_unit)
    tokens = {
        *[str(value) for value in npu.get("_tokens", []) if isinstance(value, str)],
        *[str(value) for value in accelerator.get("_tokens", []) if isinstance(value, str)],
    }
    return tokens, npu, accelerator


def _npu_score(tokens: set[str], observations: dict[str, Any]) -> tuple[int, list[str]]:
    flags = observations.get("feature_flags") if isinstance(observations.get("feature_flags"), dict) else {}
    loops = int(observations.get("loop_count") or 0)
    arrays = int(observations.get("array_access_count") or 0)
    reasons: list[str] = []
    score = 0
    explicit = tokens.intersection({"npu", "nnrt", "hiai", "mindspore", "oh_nn", "oh_nn_ops"})
    if explicit:
        score += 35
        reasons.append(f"显式 NPU/NNRT 标识：{', '.join(sorted(explicit)[:3])}")
    if observations.get("has_loop_carried_array_dependency") and not explicit:
        return 0, ["观察到 loop-carried 数组依赖，不能按普通 NNRT 图算子路由"]
    if loops >= 2 and arrays >= 2 and observations.get("has_multiply_accumulate"):
        score += 30
        reasons.append("观察到嵌套规则数组访问与乘加累积")
    if flags.get("convolution_terms") and loops >= 2 and arrays >= 2:
        score += 26
        reasons.append("观察到卷积/邻域语义、嵌套循环和数组访问")
    if flags.get("tensor_or_image_terms") and loops >= 1 and arrays >= 1:
        named_families = tokens.intersection(
            {
                "avg_pool",
                "average_pool",
                "batch_norm",
                "conv2d",
                "depthwise_conv2d",
                "fully_connected",
                "layer_norm",
                "max_pool",
                "matmul",
                "reduce_sum",
                "resize",
                "resize_bilinear",
                "softmax",
                "tensor_add",
                "transpose",
            }
        )
        if named_families:
            score += 22
            reasons.append(f"观察到张量/图像数据与算子语义：{', '.join(sorted(named_families)[:3])}")
        elif any(bool(flags.get(name)) for name in ("activation_terms", "normalization_terms", "reduction_terms", "shape_layout_terms", "indexing_terms")):
            score += 18
            reasons.append("观察到张量/图像数据、规则数组访问和图算子语义")
        elif any(bool(observations.get(name)) for name in ("has_elementwise_add", "has_elementwise_sub", "has_elementwise_mul", "has_elementwise_div")):
            score += 14
            reasons.append("观察到张量/图像数据下的规则逐元素读写")
    return score, reasons


def _gpu_score(tokens: set[str], observations: dict[str, Any]) -> tuple[int, list[str]]:
    flags = observations.get("feature_flags") if isinstance(observations.get("feature_flags"), dict) else {}
    reasons: list[str] = []
    score = 0
    explicit = tokens.intersection({"astc", "glsl", "opencl", "opengl", "shader", "spirv", "vulkan", "vkcmddispatch", "vkcmdblitimage"})
    if explicit:
        score += 30
        reasons.append(f"显式 GPU/图形计算标识：{', '.join(sorted(explicit)[:3])}")
    if flags.get("gpu_image_data") and flags.get("gpu_transform"):
        score += 24
        reasons.append("观察到图像/像素数据和颜色、几何或压缩变换")
    if flags.get("gpu_compute") and (flags.get("gpu_image_data") or flags.get("pixel_loop")):
        score += 22
        reasons.append("观察到 compute/shader 与可并行图像或像素数据")
    if flags.get("gpu_image_data") and flags.get("gpu_transfer"):
        score += 18
        reasons.append("观察到图像数据和上传、下载或拷贝边界")
    return score, reasons


def _dsp_score(tokens: set[str], observations: dict[str, Any]) -> tuple[int, list[str]]:
    flags = observations.get("feature_flags") if isinstance(observations.get("feature_flags"), dict) else {}
    reasons: list[str] = []
    score = 0
    explicit = tokens.intersection({"audio_dsp", "dsp", "effectprocess", "effectreverse"})
    if explicit:
        score += 30
        reasons.append(f"显式 DSP/音效处理标识：{', '.join(sorted(explicit)[:3])}")
    if flags.get("audio_data") and flags.get("audio_effect"):
        score += 34
        reasons.append("观察到 PCM/sample/frame 数据和音效处理语义")
    if flags.get("audio_effect") and flags.get("audio_control"):
        score += 14
        reasons.append("观察到音效处理与 provider/controller/HDI 控制面")
    return score, reasons


def _media_score(tokens: set[str], observations: dict[str, Any]) -> tuple[int, list[str]]:
    flags = observations.get("feature_flags") if isinstance(observations.get("feature_flags"), dict) else {}
    reasons: list[str] = []
    score = 0
    if flags.get("media_jpeg"):
        score += 42
        reasons.append("观察到 JPEG 与图像/码流处理语义")
    if flags.get("media_heif"):
        score += 42
        reasons.append("观察到 HEIF/HEIC 与图像解码语义")
    if flags.get("media_hcodec") or flags.get("media_codec_component"):
        score += 38
        reasons.append("观察到 HCodec/Codec Component HDI 标识")
    explicit_video = tokens.intersection({"avcodec", "hcodec", "video", "videodecoder", "videoencoder"})
    if flags.get("media_codec") and (flags.get("media_decoder") or flags.get("media_encoder")) and (
        flags.get("media_capability") or flags.get("media_format_or_surface") or explicit_video
    ):
        score += 28
        reasons.append("观察到视频 codec 生命周期及 capability/format/surface 约束")
    return score, reasons


def _unit_score(unit_id: str, tokens: set[str], npu: dict[str, Any], accelerator: dict[str, Any]) -> tuple[int, list[str]]:
    if unit_id == "npu":
        return _npu_score(tokens, npu)
    if unit_id == "gpu":
        return _gpu_score(tokens, accelerator)
    if unit_id == "dsp":
        return _dsp_score(tokens, accelerator)
    if unit_id == "media":
        return _media_score(tokens, accelerator)
    return 0, []


def _description_score(unit_id: str, record: dict[str, Any], tokens: set[str]) -> tuple[int, list[str], list[str]]:
    """Score a concrete hardware description only after unit-level routing."""

    record_tokens = _record_description_tokens(record)
    matched = sorted(
        term for term in tokens.intersection(record_tokens)
        if term not in _GENERIC_DESCRIPTION_TERMS and len(term) >= 3
    )
    score = min(len(matched), 4) * 3
    reasons = [f"硬件描述命中：{', '.join(matched[:4])}"] if matched else []
    hardware_id = str(record.get("hardware_id") or record.get("record_id") or "").upper()

    if unit_id == "npu":
        if ("HIAI" in hardware_id or "DDK" in hardware_id) and tokens.intersection({"hiai", "mindspore", "delegate"}):
            score += 12
            reasons.append("硬件描述与 HiAI/MindSpore 路径相符")
        elif "NNRT" in hardware_id:
            score += 7
            reasons.append("将通用图算子优先路由到 OpenHarmony NNRT 描述")
    elif unit_id == "gpu":
        if "OPENCL" in hardware_id and tokens.intersection({"astc", "opencl"}):
            score += 16
            reasons.append("ASTC/OpenCL 语义匹配 OpenCL 路径")
        elif "OPENGL" in hardware_id and tokens.intersection({"opengl", "rotate", "scale"}):
            score += 16
            reasons.append("几何/GL 语义匹配 OpenGL 路径")
        elif "SOURCE_PATH" in hardware_id and tokens.intersection({"brightness", "contrast", "crop", "screenshot"}):
            score += 16
            reasons.append("图像效果语义匹配已知系统图像处理路径")
    elif unit_id == "media":
        if "JPEG" in hardware_id and "jpeg" in tokens:
            score += 16
            if "ENCODER" in hardware_id and tokens.intersection({"encode", "encoder"}):
                score += 8
            if "DECODER" in hardware_id and tokens.intersection({"decode", "decoder"}):
                score += 8
            reasons.append("JPEG 语义匹配对应图像编解码硬件描述")
        elif "HEIF" in hardware_id and tokens.intersection({"heif", "heic"}):
            score += 20
            reasons.append("HEIF/HEIC 语义匹配硬件解码描述")
        elif "HCODEC" in hardware_id and tokens.intersection({"hcodec", "component"}):
            score += 20
            reasons.append("HCodec/Component 语义匹配 Codec Component HDI 描述")
        elif "VIDEO_DECODER" in hardware_id and tokens.intersection({"avcodec", "video", "decoder"}):
            score += 16
            reasons.append("视频解码语义匹配硬件 decoder 描述")
    elif unit_id == "dsp":
        if "AUDIO_EFFECT" in hardware_id:
            score += 12
            reasons.append("PCM effect 语义匹配 Audio Effect HDI DSP 描述")
    return score, matched[:8], reasons


def _hardware_record_candidates(hardware_records: list[dict[str, Any]], unit_id: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in hardware_records:
        if not isinstance(record, dict):
            continue
        # The KB also keeps source capability observations.  They are useful
        # audit material but are not a complete hardware/API contract and must
        # not route a model into an API catalog.
        record_type = str(record.get("record_type") or "")
        if record_type and record_type != "hardware_unit":
            continue
        if _canonical_unit(record) == unit_id:
            candidates.append(record)
    return candidates


def route_hardware_units(
    code_unit: dict[str, Any],
    hardware_records: list[dict[str, Any]],
    *,
    max_units: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select the primary hardware unit before querying API Top-N records.

    The default is deliberately one unit.  A caller that owns an explicit
    multi-path analysis may request more, but the normal reasoning path uses a
    single unit so it never pays the token cost of unrelated GPU/DSP/media/NPU
    catalogs.
    """

    if max_units < 1:
        raise ValueError("max_units must be at least one")
    tokens, npu_observations, accelerator_observations = _code_observations(code_unit)
    candidates: list[dict[str, Any]] = []
    for unit_id in sorted(ROUTABLE_UNITS):
        semantic_score, semantic_reasons = _unit_score(unit_id, tokens, npu_observations, accelerator_observations)
        if semantic_score < _MIN_ROUTE_SCORE:
            continue
        records = _hardware_record_candidates(hardware_records, unit_id)
        if not records:
            continue
        ranked_records = []
        for record in records:
            description_score, match_terms, description_reasons = _description_score(unit_id, record, tokens)
            ranked_records.append((description_score, str(record.get("hardware_id") or record.get("record_id") or ""), match_terms, description_reasons, record))
        description_score, _, match_terms, description_reasons, selected_record = sorted(
            ranked_records,
            key=lambda item: (-item[0], item[1]),
        )[0]
        candidates.append(
            {
                "unit_id": unit_id,
                "score": semantic_score + min(description_score, 24),
                "semantic_score": semantic_score,
                "description_score": description_score,
                "match_terms": match_terms,
                "reasons": [*semantic_reasons, *description_reasons],
                "record": selected_record,
            }
        )
    candidates.sort(
        key=lambda item: (
            -int(item["score"]),
            str((item["record"] or {}).get("hardware_id") or (item["record"] or {}).get("record_id") or ""),
        )
    )
    selected = candidates[:max_units]
    selected_summary = [
        {
            "unit_id": item["unit_id"],
            "hardware_id": str(item["record"].get("hardware_id") or item["record"].get("record_id") or ""),
            "hardware_name": str(item["record"].get("name") or item["record"].get("hardware_id") or ""),
            "evidence_id": f"hardware:{item['record'].get('hardware_id') or item['record'].get('record_id')}",
            "score": item["score"],
            "match_terms": item["match_terms"],
            "reasons": item["reasons"][:5],
        }
        for item in selected
    ]
    routing = {
        "method": "hardware_unit_semantic_router/v1",
        "status": "selected" if selected else "no_route",
        "candidate_unit_count": len(candidates),
        "selected_units": selected_summary,
        "source_observations": {
            "npu": _public_observations(npu_observations),
            "accelerator": _public_observations(accelerator_observations),
        },
        "statement_zh": (
            "先使用可见代码形态和硬件单元描述选择主单元；只在该单元的 API 摘要中召回 Top-N，"
            "再由模型核验语义、输入输出、数据生命周期和运行时门禁。未选择单元时不扫描所有 API 目录。"
        ),
    }
    return selected, routing
