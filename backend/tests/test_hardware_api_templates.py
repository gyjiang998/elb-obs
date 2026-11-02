from __future__ import annotations

import json
import unittest
from pathlib import Path


class HardwareApiTemplateCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2] / "agent-resources" / "hardware-api-templates"
        self.catalog = json.loads((self.root / "catalog.json").read_text(encoding="utf-8"))

    def test_catalog_entries_are_versioned_bounded_and_materialized(self) -> None:
        self.assertEqual(self.catalog["schema_version"], "hardware_api_template_catalog/v1")
        templates = self.catalog["templates"]
        template_ids = [item["template_id"] for item in templates]
        self.assertEqual(len(template_ids), len(set(template_ids)))
        self.assertGreaterEqual(len(templates), 5)

        allowed_levels = {
            "device_function_verified",
            "device_negative_observation",
            "source_contract_verified",
            "integration_recipe",
        }
        for item in templates:
            with self.subTest(template_id=item["template_id"]):
                self.assertIn(item["verification"]["level"], allowed_levels)
                self.assertIn(item["coverage_level"], {
                    "executable_harness",
                    "lifecycle_with_adapter",
                    "integration_recipe",
                })
                self.assertTrue(item["covered_semantic_families"])
                self.assertTrue(item["semantic_match"])
                self.assertTrue(item["adaptation_points"])
                for relative in item["files"]:
                    path = (self.root / relative).resolve()
                    path.relative_to(self.root.resolve())
                    self.assertTrue(path.is_file(), relative)

    def test_template_evidence_levels_prevent_false_success_claims(self) -> None:
        by_id = {item["template_id"]: item for item in self.catalog["templates"]}

        self.assertEqual(
            by_id["gpu.vulkan.compute-buffer.v1"]["verification"]["level"],
            "device_function_verified",
        )
        self.assertEqual(
            by_id["npu.nnrt.graph-lifecycle.v1"]["verification"]["level"],
            "device_negative_observation",
        )
        self.assertIn(
            "NOT_SUPPORTED",
            " ".join(by_id["npu.nnrt.graph-lifecycle.v1"]["verification"]["limitations"]),
        )
        self.assertEqual(
            by_id["media.avcodec.hardware-capability.v1"]["verification"]["level"],
            "source_contract_verified",
        )
        self.assertEqual(
            by_id["gpu.gles31.compute-ssbo.v1"]["coverage_level"],
            "executable_harness",
        )
        self.assertEqual(
            by_id["media.hcodec.component.recipe.v1"]["coverage_level"],
            "integration_recipe",
        )

    def test_curated_assets_do_not_embed_private_run_or_device_identity(self) -> None:
        content = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self.root.rglob("*")
            if path.is_file()
        )

        self.assertNotIn("/Users/", content)
        self.assertNotIn(".local-artifacts", content)
        self.assertNotIn("Maleoon 920", content)
        self.assertNotIn("art_", content)
        self.assertNotIn("ComputeGradientMap", content)

    def test_vulkan_template_matches_current_build_profile_contract(self) -> None:
        source = (self.root / "gpu-vulkan-compute" / "host_template.c").read_text(encoding="utf-8")

        self.assertIn('#include "experiment_shader_spv.h"', source)
        self.assertIn('dlopen("libvulkan.so"', source)
        self.assertIn("#include <sys/time.h>", source)
        self.assertIn("gettimeofday", source)
        self.assertIn("kExperimentShaderSpv", source)
        self.assertIn("int main(void)", source)
        self.assertNotIn("clock_gettime", source)
        self.assertNotIn("CLOCK_MONOTONIC", source)
        self.assertNotIn("__libc_start_main", source)


if __name__ == "__main__":
    unittest.main()
