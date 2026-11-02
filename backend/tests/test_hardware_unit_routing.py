from __future__ import annotations

import unittest

from backend.hardware_unit_routing import route_hardware_units
from backend.reasoning_lab import pasted_code_unit


def _hardware(
    hardware_id: str,
    unit_type: str,
    *,
    name: str = "",
    software_interface: str = "",
    supported_algorithms: list[str] | None = None,
) -> dict[str, object]:
    return {
        "record_id": hardware_id,
        "record_type": "hardware_unit",
        "hardware_id": hardware_id,
        "unit_type": unit_type,
        "name": name or hardware_id,
        "software_interface": software_interface,
        "supported_algorithms": supported_algorithms or [],
    }


class HardwareUnitRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            _hardware("KIRIN_NPU_NNRT", "npu", software_interface="OpenHarmony NNRT", supported_algorithms=["inference_subgraph"]),
            _hardware("KIRIN_NPU_HIAI_DDK", "npu", software_interface="HiAI DDK / MindSpore Lite delegate", supported_algorithms=["mindspore_mat_mul_fusion"]),
            _hardware("GPU_SOURCE_PATH", "gpu", software_interface="OpenHarmony image processing", supported_algorithms=["image_brightness_adjustment", "image_contrast_adjustment"]),
            _hardware("GPU_OPENCL", "gpu_compute", software_interface="OpenCL", supported_algorithms=["astc_texture_encode"]),
            _hardware("AUDIO_EFFECT_DSP_HDI", "audio_dsp", software_interface="Audio Effect HDI", supported_algorithms=["audio_effect_processing"]),
            _hardware("JPEG_HW_ENCODER", "image_codec", software_interface="Image Framework hardware encode", supported_algorithms=["jpeg_encode"]),
            _hardware("JPEG_HW_DECODER", "image_codec", software_interface="Image Framework hardware decode", supported_algorithms=["jpeg_decode"]),
            _hardware("HEIF_HW_DECODER", "image_codec", software_interface="HEIF hardware decode", supported_algorithms=["heif_primary_image_decode"]),
            _hardware("HCODEC_HDI", "video_codec", software_interface="HCodec HDI", supported_algorithms=["video_codec_create_by_mime"]),
            _hardware("VIDEO_DECODER_HARDWARE", "video_codec", software_interface="AVCodec hardware decoder", supported_algorithms=["video_decode"]),
        ]

    def _selected(self, code: str, function_hint: str) -> tuple[list[dict[str, object]], dict[str, object]]:
        return route_hardware_units(
            pasted_code_unit(code, function_hint, function_hint),
            self.records,
        )

    def test_matrix_multiply_routes_to_nnrt_before_any_api_search(self) -> None:
        selected, routing = self._selected(
            """void MatMul(const float *a, const float *b, float *out, int n) {
                for (int i = 0; i < n; ++i) for (int j = 0; j < n; ++j) {
                    float sum = 0; for (int k = 0; k < n; ++k) sum += a[i * n + k] * b[k * n + j];
                    out[i * n + j] = sum;
                }
            }""",
            "MatMul",
        )

        self.assertEqual([item["unit_id"] for item in selected], ["npu"])
        self.assertEqual(selected[0]["record"]["hardware_id"], "KIRIN_NPU_NNRT")
        self.assertEqual(routing["selected_units"][0]["unit_id"], "npu")

    def test_rgba_brightness_routes_only_to_gpu(self) -> None:
        selected, routing = self._selected(
            "void ApplyBrightnessRGBA8888(uint8_t *rgba, size_t pixels, uint8_t brightness) { for (size_t i = 0; i < pixels * 4; ++i) rgba[i] += brightness; }",
            "ApplyBrightnessRGBA8888",
        )

        self.assertEqual([item["unit_id"] for item in selected], ["gpu"])
        self.assertEqual(selected[0]["record"]["hardware_id"], "GPU_SOURCE_PATH")
        self.assertEqual(routing["candidate_unit_count"], 1)

    def test_pcm_effect_routes_only_to_dsp(self) -> None:
        selected, routing = self._selected(
            "void ProcessAudioReverb(float *pcm, uint32_t frameCount, float reverbGain) { for (uint32_t frame = 0; frame < frameCount; ++frame) pcm[frame] *= reverbGain; }",
            "ProcessAudioReverb",
        )

        self.assertEqual([item["unit_id"] for item in selected], ["dsp"])
        self.assertEqual(selected[0]["record"]["hardware_id"], "AUDIO_EFFECT_DSP_HDI")
        self.assertEqual(routing["selected_units"][0]["hardware_id"], "AUDIO_EFFECT_DSP_HDI")

    def test_jpeg_encode_routes_only_to_image_codec_media(self) -> None:
        selected, routing = self._selected(
            "bool EncodeJpeg(PixelMap *pixelMap, Stream &out) { return EncodeImageByPixelMap(pixelMap, out); }",
            "EncodeJpeg",
        )

        self.assertEqual([item["unit_id"] for item in selected], ["media"])
        self.assertEqual(selected[0]["record"]["hardware_id"], "JPEG_HW_ENCODER")
        self.assertEqual(routing["selected_units"][0]["unit_id"], "media")

    def test_sync_ipc_does_not_select_any_hardware_api_catalog(self) -> None:
        selected, routing = self._selected(
            "bool BundleProxy::GetNameForUid(int uid) { MessageParcel data; data.WriteInt32(uid); return SendTransactCmd(uid, data); }",
            "BundleProxy::GetNameForUid",
        )

        self.assertEqual(selected, [])
        self.assertEqual(routing["status"], "no_route")
        self.assertEqual(routing["selected_units"], [])


if __name__ == "__main__":
    unittest.main()
