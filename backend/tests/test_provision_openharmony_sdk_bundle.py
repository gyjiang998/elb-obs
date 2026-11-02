from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.tools.provision_openharmony_sdk_bundle import (
    SDK_DIRECTORY_PATHS,
    SDK_FILE_PATHS,
    inspect_sdk_bundle,
    provision,
    resolve_destination,
)
from backend.tools.deployment_preflight import _sdk_header_check


class ProvisionOpenHarmonySdkBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "checkout" / "openharmony"
        for index, relative in enumerate(SDK_DIRECTORY_PATHS):
            directory = self.source / relative
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"fixture_{index}.h").write_text(
                f"/* fixture {index} */\n",
                encoding="utf-8",
            )
        for index, relative in enumerate(SDK_FILE_PATHS):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"TYPEDEF unsigned long fixture_{index};\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_runtime_root_installs_fixed_openharmony_sdk_directory(self) -> None:
        runtime_root = self.root / "heterogeneous-enable-web-runtime"
        destination = resolve_destination(destination=None, runtime_root=runtime_root)

        result = provision(source_root=self.source.parent, destination=destination)

        self.assertEqual(destination, runtime_root.resolve() / "openharmony-sdk")
        self.assertEqual(result["sdk_root"], str(destination))
        self.assertEqual(
            result["environment"],
            {"HETERO_OPENHARMONY_SDK_ROOT": str(destination)},
        )
        manifest = json.loads((destination / "bundle-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "openharmony_sdk_header_bundle/v1")
        self.assertEqual(manifest["file_count"], len(SDK_DIRECTORY_PATHS) + len(SDK_FILE_PATHS))

    def test_reprovision_replaces_bundle_without_stale_headers(self) -> None:
        destination = self.root / "runtime" / "openharmony-sdk"
        destination.mkdir(parents=True)
        stale = destination / "stale.h"
        stale.write_text("stale\n", encoding="utf-8")

        provision(source_root=self.source, destination=destination)

        self.assertFalse(stale.exists())
        self.assertTrue(destination.joinpath("bundle-manifest.json").is_file())

    def test_bundle_inspection_returns_manifest_summary_not_file_inventory(self) -> None:
        destination = self.root / "runtime" / "openharmony-sdk"
        provision(source_root=self.source, destination=destination)

        inspection = inspect_sdk_bundle(destination)

        self.assertEqual(
            inspection["manifest"]["file_count"],
            len(SDK_DIRECTORY_PATHS) + len(SDK_FILE_PATHS),
        )
        self.assertNotIn("files", inspection["manifest"])

    def test_missing_sdk_surface_is_rejected_before_destination_changes(self) -> None:
        destination = self.root / "runtime" / "openharmony-sdk"
        destination.mkdir(parents=True)
        marker = destination / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")
        missing_source = self.root / "missing"
        missing_source.mkdir()

        with self.assertRaisesRegex(ValueError, "missing required SDK paths"):
            provision(source_root=missing_source, destination=destination)

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_device_preflight_checks_the_same_complete_sdk_surface(self) -> None:
        destination = self.root / "runtime" / "openharmony-sdk"
        provision(source_root=self.source, destination=destination)

        ready, detail = _sdk_header_check(destination)
        self.assertTrue(ready, detail)
        (destination / SDK_FILE_PATHS[0]).unlink()
        ready, detail = _sdk_header_check(destination)

        self.assertFalse(ready)
        self.assertIn(SDK_FILE_PATHS[0].as_posix(), detail)
        self.assertIn("install --source-root", detail)


if __name__ == "__main__":
    unittest.main()
