"""Conservative static retrieval for GPU, DSP, and media accelerator APIs.

Unlike NNRT, these accelerator integrations do not share one graph-operation
contract.  This retriever therefore requires positive *unit-domain* evidence
before returning a source-derived API entrypoint.  A generic loop, an IPC
wrapper, or a broad word such as ``buffer`` is not enough to produce a GPU,
DSP, or hardware-codec recommendation.
"""

from __future__ import annotations

import re
from typing import Any


ACCELERATOR_UNITS = frozenset({"gpu", "dsp", "media"})
_GENERIC_ALIASES = frozenset(
    {
        "audio_effect",
        "codec",
        "decode",
        "encode",
        "mime",
        "pcm",
        "video_decode",
        "video_encode",
    }
)


def _text_for_unit(code_unit: dict[str, Any]) -> str:
    hotspot = code_unit.get("hotspot") if isinstance(code_unit.get("hotspot"), dict) else {}
    raw = " ".join(
        (
            str(code_unit.get("function_hint") or ""),
            str(hotspot.get("event_name") or ""),
            str(code_unit.get("code") or ""),
        )
    )
    # Preserve member-access separators as token boundaries.  Only C++ scope
    # qualification is collapsed so ``HCodec::Create`` remains one symbol.
    normalized = raw.replace("::", "_")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized).casefold()


def _tokens(text: str) -> set[str]:
    raw_tokens = re.findall(r"[a-z][a-z0-9_]{1,}", text)
    parts = [piece for token in raw_tokens for piece in token.split("_") if len(piece) >= 2]
    return set(raw_tokens) | set(parts)


def _has_any(tokens: set[str], values: set[str]) -> bool:
    return bool(tokens.intersection(values))


def static_accelerator_observations(code_unit: dict[str, Any]) -> dict[str, Any]:
    """Extract bounded lexical evidence for accelerator-domain retrieval.

    This result is intentionally called a lexical static observation rather
    than AST extraction.  Function-level AST facts remain the responsibility
    of the precise source-analysis pipeline.
    """

    code = str(code_unit.get("code") or "")
    tokens = _tokens(_text_for_unit(code_unit))
    loop_count = len(re.findall(r"\b(?:for|while|do)\b", code))
    array_access_count = len(re.findall(r"\[[^\]\n]{1,80}\]", code))
    pixel_loop = loop_count >= 1 and array_access_count >= 1
    image_terms = {
        "argb", "bitmap", "buffer", "color", "framebuffer", "image", "nativebuffer", "pixel", "pixelmap",
        "pixels", "rgba", "rgba8888", "rgb", "stride", "surface", "texture",
    }
    image_codec_data_terms = image_terms | {
        "ashmem", "bitstream", "codecimagebuffer", "heic", "heif", "jpeg", "jpg", "nv12", "nv21",
        "rgba", "stream", "surfacebuffer", "tile", "yuv",
    }
    return {
        "analysis_method": "accelerator_api_lexical_signal/v1",
        "loop_count": loop_count,
        "array_access_count": array_access_count,
        "feature_flags": {
            "pixel_loop": pixel_loop,
            "gpu_image_data": _has_any(tokens, image_terms),
            "gpu_transform": _has_any(tokens, {"astc", "blit", "brightness", "compress", "contrast", "crop", "resize", "rotate", "scale", "shader", "texture"}),
            "gpu_compute": _has_any(tokens, {"compute", "dispatch", "fragment", "pipeline", "shader", "spirv", "workgroup"}),
            "gpu_transfer": _has_any(tokens, {"blit", "copy", "dma", "download", "nativebuffer", "readback", "upload"}),
            "audio_data": _has_any(tokens, {"audio", "channel", "frame", "framecount", "pcm", "sample", "samplerate"}),
            "audio_effect": _has_any(tokens, {"effect", "effectchain", "equalizer", "filter", "mix", "reverb", "spatial", "volume"}),
            "audio_control": _has_any(tokens, {"command", "controller", "descriptor", "factory", "hdi", "provider"}),
            "media_codec": _has_any(tokens, {"avcodec", "codec", "decoder", "encoder", "video"}),
            "media_decoder": _has_any(tokens, {"decode", "decoder", "demux", "stream"}),
            "media_encoder": _has_any(tokens, {"bitrate", "encode", "encoder", "gop"}),
            "media_capability": _has_any(tokens, {"capability", "hardware", "mime", "profile", "software"}),
            "media_format_or_surface": _has_any(tokens, {"avformat", "format", "height", "pixel", "surface", "width"}),
            "media_image_data": _has_any(tokens, image_codec_data_terms),
            "media_jpeg": _has_any(tokens, {"jpeg", "jpg"}),
            "media_heif": _has_any(tokens, {"heic", "heif", "hvc1"}),
            "media_hcodec": _has_any(tokens, {"hcodec", "hdecoder", "hencoder"}),
            "media_codec_component": _has_any(tokens, {"component", "componentid", "codeccomponent", "icodec"}),
        },
        "_tokens": sorted(tokens),
    }


def _matched_aliases(record: dict[str, Any], tokens: set[str]) -> list[str]:
    match = record.get("code_feature_match") if isinstance(record.get("code_feature_match"), dict) else {}
    aliases = match.get("operator_aliases") if isinstance(match.get("operator_aliases"), list) else []
    token_forms = tokens | {token.replace("_", "") for token in tokens}
    matched: list[str] = []
    for alias in aliases:
        if not isinstance(alias, str):
            continue
        normalized = alias.casefold()
        if normalized in _GENERIC_ALIASES:
            continue
        if normalized in token_forms or normalized.replace("_", "") in token_forms:
            matched.append(normalized)
    return matched[:8]


def _is_explicit_api_alias(symbol: str, direct_aliases: list[str]) -> bool:
    """Tell a real API symbol apart from a generic semantic alias.

    ``resize`` is a valid image-operation alias but also appears in ordinary
    container code.  Exact API symbols (for example ``vkCmdBlitImage``) remain
    meaningful without a separate image variable; a bare generic alias does
    not.
    """

    normalized_symbol = re.sub(r"[^a-z0-9]", "", symbol.casefold())
    return any(re.sub(r"[^a-z0-9]", "", alias.casefold()) == normalized_symbol for alias in direct_aliases)


def _family_score(unit_id: str, family_id: str, symbol: str, flags: dict[str, Any], direct_aliases: list[str]) -> tuple[int, list[str]]:
    # Prefer a source symbol-specific alias (for example ``effectreverse``)
    # over a broad data-shape alias such as ``framecount`` when both satisfy
    # the same DSP family.  The latter remains useful as a fallback when the
    # code contains no explicit API-like operation name.
    alias_specificity = max((len(re.sub(r"[^a-z0-9]", "", alias)) for alias in direct_aliases), default=0)
    direct_score = min(len(direct_aliases), 3) * 9 + max(0, min(alias_specificity, 16) - 8)
    reasons = [f"标识符/接口别名命中：{', '.join(direct_aliases)}"] if direct_aliases else []
    direct = bool(direct_aliases)
    explicit_api = _is_explicit_api_alias(symbol, direct_aliases)

    if unit_id == "gpu":
        image = bool(flags.get("gpu_image_data"))
        transform = bool(flags.get("gpu_transform"))
        compute = bool(flags.get("gpu_compute"))
        transfer = bool(flags.get("gpu_transfer"))
        pixel_loop = bool(flags.get("pixel_loop"))
        # Existing system image effects have different algorithms (for example
        # ASTC block encoding versus brightness).  A shared image-family signal
        # must never make one effect look like another; only a record-specific
        # alias may select them.
        if family_id == "gpu_image_effect" and direct:
            if image and (transform or pixel_loop):
                reasons.append("观察到图像/像素数据以及匹配的颜色、几何或压缩语义")
                return direct_score + 9, reasons
            return (direct_score, reasons) if explicit_api else (0, [])
        if family_id == "gpu_image_transfer" and direct:
            if image and (transfer or transform):
                reasons.append("观察到图像数据与匹配的上传、下载、拷贝或几何转换信号")
                return direct_score + 8, reasons
            return (direct_score, reasons) if explicit_api else (0, [])
        if family_id == "gpu_compute_pipeline" and (direct or (symbol == "vkCmdDispatch" and compute and (image or pixel_loop))):
            if compute and (image or pixel_loop):
                reasons.append("观察到 compute/shader/workgroup 与可并行数据形态")
                return direct_score + 8, reasons
            return (direct_score, reasons) if explicit_api else (0, [])
        return 0, []

    if unit_id == "dsp":
        audio_data = bool(flags.get("audio_data"))
        effect = bool(flags.get("audio_effect"))
        control = bool(flags.get("audio_control"))
        if family_id == "dsp_effect_process" and (direct or (symbol.endswith("EffectProcess") and audio_data and effect)):
            if audio_data and effect:
                reasons.append("观察到 PCM/sample/frame 数据与效果处理语义")
            return direct_score + (9 if audio_data and effect else 0), reasons
        if family_id == "dsp_effect_control" and (direct or (effect and control)):
            if effect and control:
                reasons.append("观察到音效语义及 provider/controller/command 控制面")
            return direct_score + (7 if effect and control else 0), reasons
        if family_id == "dsp_ap_fallback" and (direct or (audio_data and effect and "effectchain" in flags.get("_tokens", set()))):
            return direct_score + (7 if audio_data and effect else 0), reasons
        return 0, []

    if unit_id == "media":
        codec = bool(flags.get("media_codec"))
        decoder = bool(flags.get("media_decoder"))
        encoder = bool(flags.get("media_encoder"))
        capability = bool(flags.get("media_capability"))
        format_or_surface = bool(flags.get("media_format_or_surface"))
        image_data = bool(flags.get("media_image_data"))
        jpeg = bool(flags.get("media_jpeg"))
        heif = bool(flags.get("media_heif"))
        hcodec = bool(flags.get("media_hcodec"))
        component = bool(flags.get("media_codec_component"))

        if family_id == "media_jpeg_hardware_encode":
            if direct:
                if jpeg or image_data:
                    reasons.append("观察到 JPEG/图像数据与已知系统硬件编码入口")
                    return direct_score + 11, reasons
                return direct_score, reasons
            if jpeg and encoder and image_data and symbol == "ExtEncoder::HardwareEncode":
                reasons.append("观察到 JPEG 编码、图像像素/stream 数据和可验证的软件回退边界")
                return 17, reasons
            return 0, []

        if family_id == "media_jpeg_hardware_decode":
            if direct:
                if jpeg or image_data:
                    reasons.append("观察到 JPEG/图像数据与已知 Image Codec HDI 或系统解码入口")
                    return direct_score + 11, reasons
                return direct_score, reasons
            if jpeg and decoder and image_data and symbol == "ExtDecoder::DoHardWareDecode":
                reasons.append("观察到 JPEG bitstream、解码和输出图像/buffer 语义")
                return 17, reasons
            return 0, []

        if family_id == "media_heif_hardware_decode":
            if direct:
                if heif or image_data:
                    reasons.append("观察到 HEIF/HEIC 数据与已知 Image Codec HDI 或系统解码入口")
                    return direct_score + 11, reasons
                return direct_score, reasons
            if heif and decoder and image_data and symbol == "HeifDecoderImpl::HwDecodeImage":
                reasons.append("观察到 HEIF/HEIC 解码、NativeBuffer/Ashmem 或图像输出语义")
                return 17, reasons
            return 0, []

        if family_id == "media_hcodec_capability":
            if direct:
                if hcodec or component or capability:
                    reasons.append("观察到 HCodec/Codec Component HDI 的能力或对象创建标识")
                    return direct_score + 11, reasons
                return direct_score, reasons
            if hcodec and capability and symbol == "ICodecComponentManager::GetComponentCapabilityList":
                reasons.append("观察到 HCodec 与 component capability/格式约束语义")
                return 17, reasons
            if hcodec and codec and (decoder or encoder) and symbol == "HCodec::Create":
                reasons.append("观察到 HCodec 与视频编解码对象选择语义")
                return 15, reasons
            return 0, []

        if family_id == "media_hcodec_component":
            if direct:
                if hcodec or component:
                    reasons.append("观察到 HCodec/Codec Component HDI 生命周期标识")
                    return direct_score + 11, reasons
                return direct_score, reasons
            if hcodec and component and symbol == "ICodecComponentManager::CreateComponent":
                reasons.append("观察到 HCodec 与硬件 component 创建/生命周期语义")
                return 17, reasons
            if hcodec and codec and (decoder or encoder) and symbol == "HCodecLoader::CreateByName":
                reasons.append("观察到 HCodec loader 与视频 codec 创建语义")
                return 15, reasons
            return 0, []

        if family_id == "media_system_video_decoder_fallback":
            if direct:
                if codec and decoder:
                    reasons.append("观察到系统视频 decoder capability/factory 与硬件到软件回退标识")
                    return direct_score + 10, reasons
                return direct_score, reasons
            return 0, []

        if family_id == "media_capability_selection" and (direct or (symbol == "OH_AVCodec_GetCapabilityByCategory" and codec and capability)):
            if codec and capability:
                reasons.append("观察到 codec 与 hardware/software capability/MIME 选择语义")
            return direct_score + (9 if codec and capability else 0), reasons
        if family_id == "media_decoder_lifecycle" and (direct or (symbol == "OH_VideoDecoder_Configure" and codec and decoder and format_or_surface)):
            if codec and decoder and format_or_surface:
                reasons.append("观察到视频解码与 format/surface/码流生命周期语义")
            return direct_score + (8 if codec and decoder and format_or_surface else 0), reasons
        if family_id == "media_encoder_lifecycle" and (direct or (symbol == "OH_VideoEncoder_Configure" and codec and encoder and format_or_surface)):
            if codec and encoder and format_or_surface:
                reasons.append("观察到视频编码与 format/surface/bitrate 生命周期语义")
            return direct_score + (8 if codec and encoder and format_or_surface else 0), reasons
    return 0, []


def rank_accelerator_api_candidates(
    code_unit: dict[str, Any],
    records_by_unit: dict[str, list[dict[str, Any]]],
    *,
    per_unit_limit: int = 2,
    allowed_units: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return Top-N API candidates from the selected GPU/DSP/media catalogs.

    ``allowed_units`` is the output of the Hardware-KB unit router in the
    normal reasoning path.  Keeping it optional preserves the public per-unit
    search API, while preventing a hardware strategy context from paying for
    unrelated accelerator catalogs.
    """

    observations = static_accelerator_observations(code_unit)
    tokens = set(observations.pop("_tokens", []))
    flags = observations.get("feature_flags") if isinstance(observations.get("feature_flags"), dict) else {}
    flags = {**flags, "_tokens": tokens}
    ranked_all: list[dict[str, Any]] = []
    unit_summary: dict[str, dict[str, Any]] = {}
    if allowed_units is not None:
        unknown_units = set(allowed_units).difference(ACCELERATOR_UNITS)
        if unknown_units:
            raise ValueError(f"unsupported accelerator unit(s): {', '.join(sorted(unknown_units))}")
        unit_ids = sorted(allowed_units)
    else:
        unit_ids = sorted(ACCELERATOR_UNITS)
    for unit_id in unit_ids:
        candidates: list[dict[str, Any]] = []
        for record in records_by_unit.get(unit_id, []):
            if not isinstance(record, dict) or record.get("record_type") != "accelerator_api_entrypoint":
                continue
            family = record.get("semantic_family") if isinstance(record.get("semantic_family"), dict) else {}
            direct_aliases = _matched_aliases(record, tokens)
            score, reasons = _family_score(unit_id, str(family.get("id") or ""), str(record.get("symbol") or ""), flags, direct_aliases)
            if score <= 0:
                continue
            candidates.append(
                {
                    "evidence_id": f"accelerator_api:{record.get('record_id')}",
                    "score": score,
                    "match_terms": direct_aliases,
                    "retrieval_rationale": {
                        "method": "accelerator_api_static_semantic_retrieval/v1",
                        "reasons": reasons,
                        "source_observations": observations,
                        "statement_zh": "这是基于可见源码的 GPU/DSP/媒体 API Top-N 静态候选，不是精准 AST 证明、SDK 可调用性、目标设备支持或性能收益结论。",
                    },
                    "record": record,
                }
            )
        candidates.sort(key=lambda item: (-int(item["score"]), str((item["record"] or {}).get("symbol") or "")))
        limited = candidates[:per_unit_limit]
        ranked_all.extend(limited)
        unit_summary[unit_id] = {
            "candidate_count": len(candidates),
            "returned_count": len(limited),
        }
    ranked_all.sort(
        key=lambda item: (
            -int(item["score"]),
            str((item["record"] or {}).get("accelerator_unit") or ""),
            str((item["record"] or {}).get("symbol") or ""),
        )
    )
    return ranked_all, {
        "method": "accelerator_api_static_semantic_retrieval/v1",
        "candidate_count": sum(item["candidate_count"] for item in unit_summary.values()),
        "returned_count": len(ranked_all),
        "searched_units": unit_ids,
        "unit_results": unit_summary,
        "source_observations": observations,
        "statement_zh": "仅在所选单元出现图像/GPU、PCM 音效/DSP、JPEG/HEIF 图像 codec、HCodec/Codec HDI 或视频 codec 生命周期等正向领域信号时才返回候选；后续仍须验证公开性、驱动/HDI、格式、运行时 capability 和端到端基准。",
    }
