from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.curated_templates import (
    CuratedTemplateError,
    CuratedTemplateNotFoundError,
    list_curated_templates,
    materialize_curated_template_catalog,
    read_curated_template,
    validate_curated_template_catalog,
    validate_curated_template_coverage,
)


class CuratedTemplateCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.source = Path(__file__).resolve().parents[2] / "agent-resources" / "hardware-api-templates"

    def test_repository_catalog_is_hash_pinned_and_materializes_only_registered_files(self) -> None:
        catalog, registered = validate_curated_template_catalog(self.source)
        self.assertEqual(len(catalog["templates"]), 9)
        self.assertEqual(len(registered), 11)

        source_copy = self.root / "source"
        shutil.copytree(self.source, source_copy)
        (source_copy / "unregistered-answer.c").write_text("int answer(void) { return 42; }\n", encoding="utf-8")
        destination = self.root / "workspace-assets"

        summary = materialize_curated_template_catalog(source_copy, destination)

        self.assertEqual(summary["template_count"], 9)
        self.assertEqual(summary["file_count"], 11)
        self.assertTrue((destination / "catalog.json").is_file())
        self.assertFalse((destination / "unregistered-answer.c").exists())
        materialized = json.loads((destination / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(materialized["catalog_id"], "openharmony_curated_hardware_api_templates")

    def test_hash_mismatch_rejects_catalog_before_workspace_materialization(self) -> None:
        source_copy = self.root / "tampered"
        shutil.copytree(self.source, source_copy)
        target = source_copy / "gpu-vulkan-compute" / "kernel_template.comp"
        target.write_text(target.read_text(encoding="utf-8") + "\n// tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(CuratedTemplateError, "hash mismatch"):
            materialize_curated_template_catalog(source_copy, self.root / "destination")

    def test_agent_listing_filters_metadata_before_one_exact_template_read(self) -> None:
        listing = list_curated_templates(
            self.source,
            unit="GPU",
            profile_id="gpu_vulkan",
            api_family="vulkan_compute",
        )

        self.assertEqual(listing["template_count"], 1)
        self.assertEqual(listing["templates"][0]["template_id"], "gpu.vulkan.compute-buffer.v1")
        self.assertNotIn("content", listing["templates"][0])

        bundle = read_curated_template(self.source, "gpu.vulkan.compute-buffer.v1")
        self.assertEqual(bundle["template"]["profile_id"], "gpu_vulkan")
        self.assertEqual(len(bundle["files"]), 2)
        self.assertIn('dlopen("libvulkan.so"', bundle["files"][0]["content"])
        self.assertEqual(
            bundle["files"][0]["sha256"],
            bundle["template"]["file_sha256"][bundle["files"][0]["path"]],
        )

    def test_zero_exact_match_explains_facets_and_recovers_field_confusion(self) -> None:
        catalog, _ = validate_curated_template_catalog(self.source)
        api_families = {str(item.get("api_family") or "") for item in catalog["templates"]}
        for template in catalog["templates"]:
            profile_id = str(template.get("profile_id") or "")
            if not profile_id or profile_id in api_families:
                continue
            with self.subTest(profile_id=profile_id):
                listing = list_curated_templates(self.source, api_family=profile_id)
                self.assertEqual(listing["schema_version"], "curated_template_listing/v2")
                self.assertEqual(listing["match_status"], "no_exact_match")
                self.assertEqual(listing["template_count"], 0)
                self.assertEqual(listing["filter_diagnostics"][0]["recognized_as"], ["profile_id"])
                self.assertEqual(listing["suggested_templates"][0]["profile_id"], profile_id)

        listing = list_curated_templates(self.source, api_family="gpu_vulkan")
        self.assertIn("vulkan_compute", listing["available_facets"]["api_family"])
        self.assertEqual(listing["suggested_templates"][0]["canonical_filters"]["api_family"], "vulkan_compute")

    def test_semantic_query_ranks_without_requiring_catalog_identifiers(self) -> None:
        listing = list_curated_templates(
            self.source,
            unit="gpu",
            query="规则 buffer 逐元素 RGBA 亮度",
        )

        self.assertEqual(listing["match_status"], "query_ranked")
        self.assertEqual(listing["templates"][0]["template_id"], "gpu.gles31.compute-ssbo.v1")
        self.assertIn("gpu_vulkan", listing["available_facets"]["profile_id"])

    def test_agent_cannot_read_an_unregistered_template_or_path(self) -> None:
        with self.assertRaises(CuratedTemplateNotFoundError):
            read_curated_template(self.source, "../../unregistered-answer.c")

    def test_every_hardware_api_record_maps_to_a_family_template_or_recipe(self) -> None:
        report = validate_curated_template_coverage(
            self.source,
            Path(__file__).resolve().parents[2] / "data" / "knowledge-bases" / "hardware",
        )

        self.assertEqual(report["record_count"], 204)
        self.assertEqual(report["record_counts"], {"gpu": 18, "npu": 146, "dsp": 9, "media": 31})
        self.assertEqual(report["missing_records"], [])
