from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.tools.validate_next_build import (
    ASSET_INDEX_NAME,
    NextBuildValidationError,
    retain_previous_static_assets,
    validate_next_build,
    write_asset_index,
)


class ValidateNextBuildTests(unittest.TestCase):
    def _build_tree(self, root: Path, *, include_runtime: bool = True) -> Path:
        build = root / ".next"
        (build / "server").mkdir(parents=True)
        (build / "static" / "chunks").mkdir(parents=True)
        (build / "BUILD_ID").write_text("build-test\n", encoding="utf-8")
        (build / "build-manifest.json").write_text(
            json.dumps(
                {
                    "rootMainFiles": ["static/chunks/runtime.js"],
                    "pages": {"/_app": []},
                }
            ),
            encoding="utf-8",
        )
        (build / "server" / "app-paths-manifest.json").write_text(
            json.dumps({"/page": "app/page.js"}),
            encoding="utf-8",
        )
        if include_runtime:
            (build / "static" / "chunks" / "runtime.js").write_text("runtime", encoding="utf-8")
        (build / "static" / "chunks" / "app.css").write_text("body{}", encoding="utf-8")
        return build

    def test_complete_build_is_accepted_and_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build = self._build_tree(Path(temp))
            result = validate_next_build(build)
            index = write_asset_index(build, result["current_assets"])

            self.assertEqual(result["build_id"], "build-test")
            self.assertEqual(result["referenced_asset_count"], 1)
            self.assertIn("chunks/runtime.js", index.read_text(encoding="utf-8"))

    def test_missing_manifest_asset_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build = self._build_tree(Path(temp), include_runtime=False)

            with self.assertRaisesRegex(NextBuildValidationError, "referenced static assets are missing"):
                validate_next_build(build)

    def test_only_indexed_previous_generation_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = self._build_tree(root / "current")
            previous = self._build_tree(root / "previous")
            previous_runtime = previous / "static" / "chunks" / "runtime.js"
            previous_runtime.write_text("previous-runtime", encoding="utf-8")
            retained = previous / "static" / "chunks" / "previous.js"
            retained.write_text("previous", encoding="utf-8")
            ignored = previous / "static" / "chunks" / "older.js"
            ignored.write_text("older", encoding="utf-8")
            (previous / ASSET_INDEX_NAME).write_text("chunks/runtime.js\nchunks/previous.js\n", encoding="utf-8")

            count = retain_previous_static_assets(current, previous)

            self.assertEqual(count, 1)
            self.assertEqual((current / "static" / "chunks" / "runtime.js").read_text(), "runtime")
            self.assertTrue((current / "static" / "chunks" / "previous.js").is_file())
            self.assertFalse((current / "static" / "chunks" / "older.js").exists())


if __name__ == "__main__":
    unittest.main()
