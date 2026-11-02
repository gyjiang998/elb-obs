from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.core import ArtifactStore, PlatformError, SourceConnectorManager
from backend.runtime_assets import OpenHarmonySdkAssetManager
from backend.tools.provision_openharmony_sdk_bundle import SDK_DIRECTORY_PATHS, SDK_FILE_PATHS


class OpenHarmonySdkAssetManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.source_root = self.root / "openharmony"
        for index, relative in enumerate(SDK_DIRECTORY_PATHS):
            directory = self.source_root / relative
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"fixture_{index}.h").write_text("/* sdk fixture */\n", encoding="utf-8")
        for relative in SDK_FILE_PATHS:
            path = self.source_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("TYPEDEF unsigned long fixture_size_t;\n", encoding="utf-8")
        store = ArtifactStore(self.state_root, project_id="local-default")
        self.connectors = SourceConnectorManager(self.state_root, store, workspace_root=self.root)
        self.connector = {
            "connector_id": "local_openharmony_fixture",
            "kind": "local",
            "mode": "local_readonly",
            "display_name": "OpenHarmony fixture",
            "source_root": str(self.source_root),
            "snapshot": {"snapshot_id": "source_snapshot_fixture", "clean": True, "repositories": []},
        }
        self.connectors.registry.write({"connectors": [self.connector]})
        self.manager = OpenHarmonySdkAssetManager(
            self.state_root,
            self.connectors,
            sdk_root=self.root / "assets" / "openharmony-sdk",
        )

    def test_provisions_from_connector_and_persists_selection(self) -> None:
        result = self.manager.provision_from_connector("local_openharmony_fixture")
        status = self.manager.status()

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["selection"]["source_connector_id"], "local_openharmony_fixture")
        self.assertEqual(status["bundle"]["status"], "ready")
        self.assertEqual(status["source"]["status"], "available")
        self.assertEqual(status["source"]["current_snapshot_id"], "source_snapshot_fixture")

    def test_rejects_registered_module_that_is_not_an_openharmony_checkout(self) -> None:
        module = self.root / "module-only"
        module.mkdir()
        self.connectors.registry.write({"connectors": [{**self.connector, "connector_id": "local_module", "source_root": str(module)}]})

        with self.assertRaises(PlatformError) as raised:
            self.manager.provision_from_connector("local_module")

        self.assertEqual(raised.exception.code, "OPENHARMONY_SDK_SOURCE_INCOMPLETE")
        self.assertEqual(raised.exception.details["source_connector_id"], "local_module")


if __name__ == "__main__":
    unittest.main()
