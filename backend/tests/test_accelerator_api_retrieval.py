from __future__ import annotations

import unittest

from backend import app as platform_app
from backend.accelerator_api_retrieval import rank_accelerator_api_candidates
from backend.reasoning_lab import pasted_code_unit


def _record(unit: str, symbol: str, family: str, aliases: list[str]) -> dict[str, object]:
    return {
        "record_type": "accelerator_api_entrypoint",
        "record_id": f"{unit}_api:{symbol}",
        "accelerator_unit": unit,
        "symbol": symbol,
        "semantic_family": {"id": family},
        "code_feature_match": {"operator_aliases": aliases},
    }


class AcceleratorApiRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = {
            "gpu": [
                _record("gpu", "GpuBrightnessAlgo::OnApplyRGBA8888", "gpu_image_effect", ["brightness", "rgba8888"]),
                _record("gpu", "GpuContrastAlgo::OnApplyRGBA8888", "gpu_image_effect", ["contrast", "rgba8888"]),
                _record("gpu", "vkCmdDispatch", "gpu_compute_pipeline", ["vkcmddispatch", "compute_shader"]),
            ],
            "dsp": [
                _record("dsp", "IEffectControlVdi::EffectProcess", "dsp_effect_process", ["effectprocess", "pcm"]),
                _record("dsp", "IEffectControlVdi::SendCommand", "dsp_effect_control", ["hdi_command", "sendcommand"]),
            ],
            "media": [
                _record("media", "OH_AVCodec_GetCapabilityByCategory", "media_capability_selection", ["oh_avcodec_getcapabilitybycategory", "hardware_codec"]),
                _record("media", "OH_VideoDecoder_Configure", "media_decoder_lifecycle", ["oh_videodecoder_configure", "video_decode"]),
                _record("media", "ExtEncoder::HardwareEncode", "media_jpeg_hardware_encode", ["hardwareencode"]),
                _record("media", "ExtDecoder::DoHardWareDecode", "media_jpeg_hardware_decode", ["dohardwaredecode"]),
                _record("media", "HeifDecoderImpl::HwDecodeImage", "media_heif_hardware_decode", ["hwdecodeimage"]),
                _record("media", "HCodecLoader::CreateByName", "media_hcodec_component", ["hcodecloader"]),
                _record("media", "HCodec::Create", "media_hcodec_capability", ["hcodeccreate"]),
                _record("media", "ICodecComponentManager::GetComponentCapabilityList", "media_hcodec_capability", ["getcomponentcapabilitylist"]),
            ],
        }

    def test_ipc_proxy_without_domain_signal_returns_no_accelerator_api_candidate(self) -> None:
        unit = pasted_code_unit(
            "bool BundleProxy::GetNameForUid(int uid) { MessageParcel data; return SendTransactCmd(uid, data); }",
            "BundleProxy::GetNameForUid",
            "GetNameForUid",
        )
        ranked, retrieval = rank_accelerator_api_candidates(unit, self.records)

        self.assertEqual(ranked, [])
        self.assertEqual(retrieval["candidate_count"], 0)

    def test_rgba_brightness_loop_prefers_existing_gpu_image_path(self) -> None:
        unit = pasted_code_unit(
            "void Brightness(uint8_t *rgba, size_t pixels) { for (size_t i = 0; i < pixels * 4; ++i) rgba[i] += brightness; }",
            "Brightness",
            "Brightness",
        )
        ranked, retrieval = rank_accelerator_api_candidates(unit, self.records)

        self.assertEqual([item["record"]["symbol"] for item in ranked], ["GpuBrightnessAlgo::OnApplyRGBA8888"])
        self.assertEqual(ranked[0]["record"]["accelerator_unit"], "gpu")
        self.assertGreater(retrieval["unit_results"]["gpu"]["candidate_count"], 0)
        self.assertIn("不是精准 AST 证明", ranked[0]["retrieval_rationale"]["statement_zh"])

    def test_pcm_effect_processing_returns_dsp_candidate_without_device_claim(self) -> None:
        unit = pasted_code_unit(
            "void ProcessAudioEffect(float *pcm, uint32_t frameCount) { for (uint32_t i = 0; i < frameCount; ++i) pcm[i] *= reverbGain; }",
            "ProcessAudioEffect",
            "AudioEffect",
        )
        ranked, retrieval = rank_accelerator_api_candidates(unit, self.records)

        self.assertEqual(ranked[0]["record"]["symbol"], "IEffectControlVdi::EffectProcess")
        self.assertEqual(retrieval["unit_results"]["dsp"]["returned_count"], 1)

    def test_hardware_codec_capability_api_is_matched_by_normalized_symbol(self) -> None:
        unit = pasted_code_unit(
            'auto *capability = OH_AVCodec_GetCapabilityByCategory("video/avc", false, AVCODEC_HARDWARE);',
            "CreateHardwareDecoder",
            "CreateHardwareDecoder",
        )
        ranked, _ = rank_accelerator_api_candidates(unit, self.records)

        self.assertEqual(ranked[0]["record"]["symbol"], "OH_AVCodec_GetCapabilityByCategory")
        self.assertEqual(ranked[0]["record"]["accelerator_unit"], "media")

    def test_jpeg_encode_candidate_requires_image_codec_semantics(self) -> None:
        unit = pasted_code_unit(
            "bool EncodeJpeg(PixelMap *pixelMap, SkWStream &out) { return EncodeImageByPixelMap(pixelMap, out); }",
            "EncodeJpeg",
            "EncodeJpeg",
        )
        ranked, retrieval = rank_accelerator_api_candidates(unit, self.records)

        self.assertEqual(ranked[0]["record"]["symbol"], "ExtEncoder::HardwareEncode")
        self.assertTrue(retrieval["source_observations"]["feature_flags"]["media_jpeg"])

    def test_heif_and_hcodec_are_not_confused_with_generic_codec_paths(self) -> None:
        heif_unit = pasted_code_unit(
            "bool DecodeHeif(const std::vector<Ashmem> &inputs, NativeBuffer *output) { return DecodeGrid(inputs, output); }",
            "DecodeHeif",
            "DecodeHeif",
        )
        heif_ranked, _ = rank_accelerator_api_candidates(heif_unit, self.records)
        self.assertEqual(heif_ranked[0]["record"]["symbol"], "HeifDecoderImpl::HwDecodeImage")

        hcodec_unit = pasted_code_unit(
            "auto manager = ICodecComponentManager::Get(); return manager->GetComponentCapabilityList(caps, count);",
            "HCodecCapability",
            "HCodecCapability",
        )
        hcodec_ranked, _ = rank_accelerator_api_candidates(hcodec_unit, self.records)
        self.assertEqual(hcodec_ranked[0]["record"]["symbol"], "ICodecComponentManager::GetComponentCapabilityList")

        direct_hcodec = pasted_code_unit(
            "std::shared_ptr<HCodec> HCodec::Create(const std::string &name) { return nullptr; }",
            "HCodec::Create",
            "HCodec::Create",
        )
        direct_ranked, _ = rank_accelerator_api_candidates(direct_hcodec, self.records)
        self.assertEqual(direct_ranked[0]["record"]["symbol"], "HCodec::Create")

    def test_container_resize_without_image_data_does_not_select_gpu_blit(self) -> None:
        records = {
            "gpu": [
                _record("gpu", "vkCmdBlitImage", "gpu_image_transfer", ["vkcmdblitimage", "resize"]),
            ],
            "dsp": [],
            "media": [],
        }
        unit = pasted_code_unit(
            "void ResolveBundleNames(std::vector<int> &uids) { uids.resize(64); for (size_t i = 0; i < uids.size(); ++i) Consume(uids[i]); }",
            "ResolveBundleNames",
            "ResolveBundleNames",
        )

        ranked, retrieval = rank_accelerator_api_candidates(unit, records)

        self.assertEqual(ranked, [])
        self.assertEqual(retrieval["unit_results"]["gpu"]["candidate_count"], 0)

    def test_allowed_units_keeps_unselected_accelerator_catalogs_out_of_retrieval(self) -> None:
        unit = pasted_code_unit(
            "void Brightness(uint8_t *rgba, size_t pixels) { for (size_t i = 0; i < pixels * 4; ++i) rgba[i] += brightness; }",
            "Brightness",
            "Brightness",
        )

        ranked, retrieval = rank_accelerator_api_candidates(unit, self.records, allowed_units={"gpu"}, per_unit_limit=3)

        self.assertEqual([item["record"]["accelerator_unit"] for item in ranked], ["gpu"])
        self.assertEqual(retrieval["searched_units"], ["gpu"])
        self.assertEqual(set(retrieval["unit_results"]), {"gpu"})


class AcceleratorApiCatalogTests(unittest.TestCase):
    def test_source_derived_catalogs_expose_unit_boundaries_and_records(self) -> None:
        units = platform_app.list_accelerator_api_units()
        self.assertEqual([item["accelerator_unit"] for item in units["units"]], ["dsp", "gpu", "media"])
        self.assertEqual(units["accelerator_api_manifest"]["manifest_id"], "openharmony_accelerator_api_knowledge")
        self.assertTrue(all(item["api_kb"]["summary"]["target_device_availability"] == "evidence_required" for item in units["units"]))

        gpu = platform_app.list_accelerator_api_kb_records("gpu", "brightness")
        self.assertEqual(gpu["accelerator_api_kb"]["kb_id"], "openharmony_gpu_api_knowledge")
        self.assertEqual(gpu["records"][0]["symbol"], "GpuBrightnessAlgo::OnApplyRGBA8888")
        self.assertEqual(gpu["records"][0]["exposure"], "system_source_integration")

        dsp = platform_app.accelerator_api_kb_record_detail("dsp", "dsp_api:IEffectControlVdi::EffectProcess")
        self.assertEqual(dsp["record"]["source_contract"]["exposure"], "driver_hdi_contract")
        self.assertEqual(dsp["record"]["capability_boundary"]["target_device_availability"], "evidence_required")

    def test_unit_search_reuses_conservative_static_retrieval(self) -> None:
        request = platform_app.AcceleratorApiSearchRequest(
            code="void Brightness(uint8_t *rgba, size_t pixels) { for (size_t i = 0; i < pixels * 4; ++i) rgba[i] += brightness; }",
            function_hint="Brightness",
            event_name="Brightness",
            limit=2,
        )
        result = platform_app.search_accelerator_api_kb("gpu", request)
        self.assertEqual(result["retrieval"]["method"], "accelerator_api_static_semantic_retrieval/v1")
        self.assertEqual(result["records"][0]["record"]["symbol"], "GpuBrightnessAlgo::OnApplyRGBA8888")
        self.assertEqual(result["records"][0]["record"]["capability_boundary"]["target_device_availability"], "evidence_required")


if __name__ == "__main__":
    unittest.main()
