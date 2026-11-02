from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.curated_templates import (
    CuratedTemplateError,
    materialize_curated_template_catalog,
    validate_curated_template_catalog,
)


TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "agent-resources" / "hardware-api-templates"


class CuratedHardwareApiTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = json.loads((TEMPLATE_ROOT / "catalog.json").read_text(encoding="utf-8"))

    def test_catalog_files_are_bounded_versioned_and_present(self) -> None:
        validated, registered = validate_curated_template_catalog(TEMPLATE_ROOT)
        self.assertEqual(validated["catalog_id"], self.catalog["catalog_id"])
        self.assertGreaterEqual(len(registered), 5)
        self.assertEqual(self.catalog["schema_version"], "hardware_api_template_catalog/v1")
        templates = self.catalog["templates"]
        template_ids = [item["template_id"] for item in templates]
        self.assertEqual(len(template_ids), len(set(template_ids)))

        for template in templates:
            with self.subTest(template_id=template["template_id"]):
                self.assertTrue(template["api_family"])
                self.assertTrue(template["profile_id"] or template["exposure_layer"] == "system_hdi_provider")
                self.assertGreaterEqual(len(template["adaptation_points"]), 1)
                self.assertTrue(template["verification"]["level"])
                self.assertGreaterEqual(len(template["verification"]["limitations"]), 1)
                for relative_file in template["files"]:
                    path = (TEMPLATE_ROOT / relative_file).resolve()
                    self.assertTrue(path.is_relative_to(TEMPLATE_ROOT.resolve()))
                    self.assertTrue(path.is_file())
                    self.assertGreater(path.stat().st_size, 0)

    def test_materialization_copies_only_hash_pinned_registered_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            destination = Path(temporary) / "destination"
            shutil.copytree(TEMPLATE_ROOT, source)
            (source / "unregistered-answer.c").write_text("int leaked_answer;\n", encoding="utf-8")

            summary = materialize_curated_template_catalog(source, destination)

            self.assertEqual(summary["template_count"], len(self.catalog["templates"]))
            self.assertTrue((destination / "catalog.json").is_file())
            self.assertFalse((destination / "unregistered-answer.c").exists())

    def test_registered_template_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            shutil.copytree(TEMPLATE_ROOT, source)
            template = self.catalog["templates"][0]
            registered_file = source / template["files"][0]
            registered_file.write_text(
                registered_file.read_text(encoding="utf-8") + "\n/* tampered */\n",
                encoding="utf-8",
            )

            with self.assertRaises(CuratedTemplateError):
                validate_curated_template_catalog(source)

    def test_holdout_workload_is_not_embedded_in_the_template_library(self) -> None:
        corpus = "\n".join(
            path.read_text(encoding="utf-8")
            for path in TEMPLATE_ROOT.rglob("*")
            if path.is_file()
        )

        self.assertNotIn("DetectEdges", corpus)
        self.assertNotIn("BuildEdgeMap", corpus)
        self.assertNotIn("ComputeGradientMap", corpus)

    def test_template_maturity_does_not_overclaim_negative_or_contract_examples(self) -> None:
        templates = {item["template_id"]: item for item in self.catalog["templates"]}

        self.assertEqual(
            templates["npu.nnrt.graph-lifecycle.v1"]["verification"]["level"],
            "device_negative_observation",
        )
        self.assertEqual(
            templates["media.avcodec.hardware-capability.v1"]["verification"]["level"],
            "source_contract_verified",
        )
        self.assertEqual(
            templates["dsp.audio-effect-provider.recipe.v1"]["verification"]["level"],
            "integration_recipe",
        )


if __name__ == "__main__":
    unittest.main()
