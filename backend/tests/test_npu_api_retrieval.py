from __future__ import annotations

import unittest

from backend import app as platform_app
from backend.npu_api_retrieval import rank_npu_operation_candidates
from backend.reasoning_lab import pasted_code_unit


def _operation(symbol: str, family: str, aliases: list[str]) -> dict[str, object]:
    return {
        "record_type": "npu_operation",
        "record_id": f"nnrt_op:{symbol}",
        "symbol": symbol,
        "semantic_family": {"id": family},
        "code_feature_match": {"operator_aliases": aliases},
    }


class NpuApiRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            _operation("OH_NN_OPS_MATMUL", "matrix", ["matmul", "matrix_multiply"]),
            _operation("OH_NN_OPS_FULL_CONNECTION", "matrix", ["full_connection", "fully_connected"]),
            _operation("OH_NN_OPS_CONV2D", "convolution", ["conv2d"]),
            _operation("OH_NN_OPS_DEPTHWISE_CONV2D_NATIVE", "convolution", ["depthwise_conv2d"]),
            _operation("OH_NN_OPS_MUL", "elementwise_arithmetic", ["mul"]),
            _operation("OH_NN_OPS_ADD", "elementwise_arithmetic", ["add"]),
        ]

    def test_nested_multiply_accumulate_prefers_matmul_over_broader_matrix_family(self) -> None:
        unit = pasted_code_unit(
            """void Matmul(const float *a, const float *b, float *out, int n) {
                for (int i = 0; i < n; ++i) for (int j = 0; j < n; ++j) {
                    float sum = 0; for (int k = 0; k < n; ++k) sum += a[i * n + k] * b[k * n + j];
                    out[i * n + j] = sum;
                }
            }""",
            "Matmul",
            "Matmul",
        )
        ranked, retrieval = rank_npu_operation_candidates(unit, self.records, limit=5)

        self.assertEqual(retrieval["candidate_count"], 2)
        self.assertEqual(ranked[0]["record"]["symbol"], "OH_NN_OPS_MATMUL")
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])
        self.assertEqual(ranked[1]["record"]["symbol"], "OH_NN_OPS_FULL_CONNECTION")

    def test_bare_three_level_multiply_accumulate_prefers_matmul_without_a_function_name(self) -> None:
        unit = pasted_code_unit(
            """for (int row = 0; row < n; ++row) {
                for (int col = 0; col < n; ++col) {
                    float sum = 0.0F;
                    for (int k = 0; k < n; ++k) sum += lhs[row * n + k] * rhs[k * n + col];
                    output[row * n + col] = sum;
                }
            }""",
            "",
            "pasted_code",
        )

        ranked, _ = rank_npu_operation_candidates(unit, self.records, limit=2)

        self.assertEqual([item["record"]["symbol"] for item in ranked], [
            "OH_NN_OPS_MATMUL",
            "OH_NN_OPS_FULL_CONNECTION",
        ])

    def test_explicit_full_connection_name_overrides_generic_matmul_shape(self) -> None:
        unit = pasted_code_unit(
            """void FullConnection(const float *input, const float *weight, float *output, int n) {
                for (int row = 0; row < n; ++row) for (int col = 0; col < n; ++col) {
                    float sum = 0.0F;
                    for (int k = 0; k < n; ++k) sum += input[row * n + k] * weight[k * n + col];
                    output[row * n + col] = sum;
                }
            }""",
            "FullConnection",
            "FullConnection",
        )

        ranked, _ = rank_npu_operation_candidates(unit, self.records, limit=2)

        self.assertEqual(ranked[0]["record"]["symbol"], "OH_NN_OPS_FULL_CONNECTION")

    def test_ipc_proxy_without_compute_signal_returns_no_npu_candidate(self) -> None:
        unit = pasted_code_unit(
            "bool BundleProxy::GetNameForUid(int uid) { MessageParcel data; return SendTransactCmd(uid, data); }",
            "BundleProxy::GetNameForUid",
            "GetNameForUid",
        )
        ranked, retrieval = rank_npu_operation_candidates(unit, self.records, limit=5)

        self.assertEqual(ranked, [])
        self.assertEqual(retrieval["candidate_count"], 0)

    def test_pixel_add_loop_stays_a_candidate_and_not_a_device_claim(self) -> None:
        unit = pasted_code_unit(
            "void Add(uint8_t *pixels, size_t n) { for (size_t i = 0; i < n; ++i) pixels[i] += 1; }",
            "Add",
            "Add",
        )
        ranked, retrieval = rank_npu_operation_candidates(unit, self.records, limit=5)

        self.assertEqual(retrieval["method"], "npu_api_static_semantic_retrieval/v1")
        self.assertEqual([item["record"]["symbol"] for item in ranked], ["OH_NN_OPS_ADD"])
        self.assertIn("不是 AST 完整语义证明", ranked[0]["retrieval_rationale"]["statement_zh"])

    def test_pcm_effect_and_loop_carried_prefix_sum_do_not_become_elementwise_npu_nodes(self) -> None:
        pcm = pasted_code_unit(
            "void ProcessAudioReverb(float *pcm, int frameCount) { for (int i = 0; i < frameCount; ++i) pcm[i] = pcm[i] * 0.5F; }",
            "ProcessAudioReverb",
            "ProcessAudioReverb",
        )
        prefix_sum = pasted_code_unit(
            "void PrefixSum(const int *input, int *output, int count) { output[0] = input[0]; for (int i = 1; i < count; ++i) output[i] = output[i - 1] + input[i]; }",
            "PrefixSum",
            "PrefixSum",
        )

        pcm_ranked, _ = rank_npu_operation_candidates(pcm, self.records, limit=5)
        prefix_ranked, prefix_retrieval = rank_npu_operation_candidates(prefix_sum, self.records, limit=5)

        self.assertEqual(pcm_ranked, [])
        self.assertEqual(prefix_ranked, [])
        self.assertTrue(prefix_retrieval["source_observations"]["has_loop_carried_array_dependency"])

    def test_specific_depthwise_alias_ranks_before_broader_conv_alias(self) -> None:
        unit = pasted_code_unit(
            """void depthwise_conv2d(const float *feature, const float *kernel, float *out, int n) {
                for (int y = 0; y < n; ++y) for (int x = 0; x < n; ++x) {
                    float sum = 0; for (int k = 0; k < 3; ++k) sum += feature[y * n + x] * kernel[k];
                    out[y * n + x] = sum;
                }
            }""",
            "depthwise_conv2d",
            "depthwise_conv2d",
        )

        ranked, _ = rank_npu_operation_candidates(unit, self.records, limit=5)

        self.assertEqual(ranked[0]["record"]["symbol"], "OH_NN_OPS_DEPTHWISE_CONV2D_NATIVE")

    def test_atomic_fetch_add_without_tensor_domain_is_not_an_npu_add(self) -> None:
        unit = pasted_code_unit(
            "void CountReady(Task *tasks, int count, std::atomic<int> &ready) { for (int i = 0; i < count; ++i) if (tasks[i].ready) ready.fetch_add(1); }",
            "CountReady",
            "CountReady",
        )

        ranked, _ = rank_npu_operation_candidates(unit, self.records, limit=5)

        self.assertEqual(ranked, [])


class NpuApiCatalogTests(unittest.TestCase):
    def test_catalog_exposes_source_derived_operations_and_entrypoints(self) -> None:
        response = platform_app.list_npu_api_kb_records()

        self.assertEqual(response["npu_api_kb"]["kb_id"], "openharmony_npu_api_knowledge")
        self.assertEqual(response["summary"]["npu_operation_count"], 108)
        self.assertEqual(response["summary"]["npu_api_entrypoint_count"], 38)
        self.assertEqual(response["records"][0]["symbol"], "OH_NN_OPS_ADD")
        self.assertTrue(all("device_availability" in item for item in response["records"]))

    def test_catalog_query_and_detail_preserve_runtime_support_boundary(self) -> None:
        response = platform_app.list_npu_api_kb_records("matmul")
        self.assertEqual([item["symbol"] for item in response["records"]], ["OH_NN_OPS_MATMUL"])

        chinese_query = platform_app.list_npu_api_kb_records("矩阵")
        self.assertEqual(chinese_query["records"][0]["symbol"], "OH_NN_OPS_MATMUL")

        detail = platform_app.npu_api_kb_record_detail("nnrt_op:OH_NN_OPS_MATMUL")
        record = detail["record"]
        self.assertEqual(record["symbol"], "OH_NN_OPS_MATMUL")
        self.assertEqual(record["capability_boundary"]["target_device_availability"], "evidence_required")
        self.assertIn("OH_NNModel_GetAvailableOperations", " ".join(record["runtime_verification"]))


if __name__ == "__main__":
    unittest.main()
