from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.core import ArtifactStore, PlatformError, SourceConnectorManager
from backend.custom_api_kb import CustomApiKnowledgeBaseManager


class CustomApiKnowledgeBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.source_root = self.root / "aae-module"
        include = self.source_root / "interfaces" / "include"
        include.mkdir(parents=True)
        (include / "aae_engine.h").write_text(
            """#ifndef AAE_ENGINE_H
#define AAE_ENGINE_H
#include <stddef.h>

/// Process one PCM frame through the accelerator effect engine.
AAE_API int AAE_ProcessPcm(const float *input, float *output, size_t frame_count);

/// Query whether the named effect is available.
AAE_API bool AAE_GetEffectCapability(const char *effect_name);

class AaeEngine {
public:
    bool Configure(unsigned int sample_rate, unsigned int channels);
    int Execute(const float *input, float *output, size_t frame_count);
};

typedef void (*AAE_Callback)(int status);
#endif
""",
            encoding="utf-8",
        )
        source = self.source_root / "src"
        source.mkdir()
        (source / "aae_client.cpp").write_text(
            """int RunAae(const float *input, float *output, unsigned int frame_count)
{
    AAE_SubmitBuffer(input, frame_count);
    return AAE_ProcessPcm(input, output, frame_count);
}
""",
            encoding="utf-8",
        )
        generated = self.source_root / ".local-artifacts" / "generated"
        generated.mkdir(parents=True)
        (generated / "aae_generated.h").write_text(
            "int AAE_HiddenGeneratedApi(const float *input);\n",
            encoding="utf-8",
        )
        store = ArtifactStore(self.state_root, project_id="local-default")
        self.connectors = SourceConnectorManager(self.state_root, store, workspace_root=self.root)
        self.connector = {
            "connector_id": "local_aae_test",
            "kind": "local",
            "mode": "local_readonly",
            "display_name": "AAE module",
            "source_root": str(self.source_root.resolve()),
            "snapshot": {"snapshot_id": "source_snapshot_aae_test", "clean": True, "repositories": []},
        }
        self.connectors.registry.write({"connectors": [self.connector]})
        self.manager = CustomApiKnowledgeBaseManager(self.state_root, self.connectors)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_builds_traceable_isolated_catalog(self) -> None:
        result = self.manager.build(
            unit_name="AAE",
            source_connector_id=str(self.connector["connector_id"]),
        )

        metadata = result["knowledge_base"]
        self.assertEqual(metadata["display_name"], "AAE API（自建）")
        self.assertEqual(metadata["origin"], "user_built")
        self.assertEqual(metadata["merge_policy"], "developer_only")
        self.assertGreaterEqual(metadata["summary"]["record_count"], 4)
        self.assertEqual(metadata["summary"]["evidence_kind_counts"]["callsite"], 1)
        self.assertEqual(len(metadata["scan"]["scanned_source_sha256"]), 64)
        self.assertFalse(metadata["scan"]["result_truncated"])

        catalog = self.manager.read(str(metadata["kb_id"]))
        symbols = {record["symbol"] for record in catalog["records"]}
        self.assertIn("AAE_ProcessPcm", symbols)
        self.assertIn("AAE_GetEffectCapability", symbols)
        self.assertIn("RunAae", symbols)
        self.assertNotIn("AAE_HiddenGeneratedApi", symbols)
        self.assertNotIn("void", symbols)
        self.assertEqual(sum(record["symbol"] == "AAE_ProcessPcm" for record in catalog["records"]), 1)
        callsite = next(item for item in catalog["records"] if item["symbol"] == "AAE_SubmitBuffer")
        self.assertEqual(callsite["record_type"], "custom_accelerator_api_callsite_candidate")
        self.assertEqual(callsite["source_contract"]["evidence_kind"], "callsite")
        record = next(item for item in catalog["records"] if item["symbol"] == "AAE_ProcessPcm")
        self.assertEqual(record["source_contract"]["source_file"], "interfaces/include/aae_engine.h")
        self.assertEqual(record["code_feature_match"]["analysis_method"], "bounded_api_contract_discovery/v2")
        self.assertEqual(record["capability_boundary"]["target_device_availability"], "evidence_required")

    def test_public_api_and_operator_contracts_outrank_internal_service_methods(self) -> None:
        source_root = self.root / "nnrt-module"
        public = source_root / "interfaces" / "kits" / "c" / "neural_network_runtime"
        public.mkdir(parents=True)
        (public / "nnrt_api.h").write_text(
            """/** Public neural-network accelerator contract. */
OH_NNModel *OH_NNModel_Construct(void);

/** Finalize the graph before compilation. */
OH_NN_ReturnCode OH_NNModel_Finish(OH_NNModel *model);

/** Operators accepted by the model builder. */
typedef enum {
    /** Matrix multiplication operator. */
    OH_NN_OPS_MATMUL = 19,
    /** Two-dimensional convolution operator. */
    OH_NN_OPS_CONV_2D = 20,
} OH_NN_OperationType;
""",
            encoding="utf-8",
        )
        internal = source_root / "example" / "drivers" / "nnrt" / "hdi_cpu_service" / "include"
        internal.mkdir(parents=True)
        (internal / "prepared_model_service.h").write_text(
            """class PreparedModelService {
public:
    int Run(const void *inputs, void *outputs);
    int Compile(const void *model);
    int SetInputs(const void *inputs);
};
""",
            encoding="utf-8",
        )
        connector = {
            "connector_id": "local_nnrt_test",
            "kind": "local",
            "mode": "local_readonly",
            "display_name": "NNRT module",
            "source_root": str(source_root.resolve()),
            "snapshot": {"snapshot_id": "source_snapshot_nnrt_test", "clean": True, "repositories": []},
        }
        self.connectors.registry.write({"connectors": [connector]})
        manager = CustomApiKnowledgeBaseManager(self.state_root, self.connectors)

        result = manager.build(unit_name="NPU", source_connector_id="local_nnrt_test")
        catalog = manager.read(str(result["knowledge_base"]["kb_id"]))
        symbols = [record["symbol"] for record in catalog["records"]]

        self.assertIn("OH_NNModel_Construct", symbols)
        self.assertIn("OH_NNModel_Finish", symbols)
        self.assertIn("OH_NN_OPS_MATMUL", symbols)
        self.assertIn("OH_NN_OPS_CONV_2D", symbols)
        self.assertNotIn("PreparedModelService::Run", symbols)
        self.assertNotIn("Run", symbols)
        summary = catalog["knowledge_base"]["summary"]
        self.assertEqual(summary["public_contract_record_count"], 4)
        self.assertEqual(summary["operator_record_count"], 2)
        self.assertEqual(summary["internal_implementation_record_count"], 0)
        self.assertEqual(catalog["knowledge_base"]["quality"]["status"], "pass")

    def test_same_unit_creates_independent_assets(self) -> None:
        first = self.manager.build(unit_name="AAE", source_root=str(self.source_root))
        second = self.manager.build(unit_name="AAE", source_root=str(self.source_root))

        self.assertNotEqual(first["knowledge_base"]["kb_id"], second["knowledge_base"]["kb_id"])
        self.assertEqual(len(self.manager.list()), 2)

    def test_user_catalog_projects_to_an_isolated_reasoning_directory(self) -> None:
        result = self.manager.build(unit_name="AAE", source_connector_id=str(self.connector["connector_id"]))
        kb_id = str(result["knowledge_base"]["kb_id"])

        catalogs = self.manager.reasoning_catalogs()
        unit_id = f"custom:{kb_id}"
        self.assertEqual(set(catalogs), {unit_id})
        catalog = catalogs[unit_id]
        self.assertEqual(catalog["kb_id"], kb_id)
        self.assertGreaterEqual(len(catalog["records"]), 4)
        self.assertEqual(catalog["routing_record"]["record_type"], "custom_hardware_api_directory")
        self.assertEqual(catalog["routing_record"]["routing_unit_id"], unit_id)
        self.assertEqual(catalog["routing_record"]["routing_evidence_id"], f"custom_api_kb:{kb_id}")
        self.assertEqual(catalog["routing_record"]["content_sha256"], catalog["metadata"]["content_sha256"])
        self.assertTrue(catalog["routing_record"]["representative_api_summaries"])

    def test_custom_asset_can_be_deleted_but_platform_id_cannot(self) -> None:
        result = self.manager.build(unit_name="AAE", source_connector_id=str(self.connector["connector_id"]))
        kb_id = str(result["knowledge_base"]["kb_id"])

        deleted = self.manager.delete(kb_id)
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(self.manager.list(), [])
        with self.assertRaises(PlatformError) as raised:
            self.manager.delete("openharmony_npu_api_knowledge")
        self.assertEqual(raised.exception.code, "CUSTOM_API_KB_DELETE_FORBIDDEN")

    def test_unregistered_path_is_rejected(self) -> None:
        other = self.root / "unregistered"
        other.mkdir()
        with self.assertRaises(PlatformError) as raised:
            self.manager.build(unit_name="XYZ", source_root=str(other))
        self.assertEqual(raised.exception.code, "CUSTOM_API_KB_SOURCE_NOT_REGISTERED")


if __name__ == "__main__":
    unittest.main()
