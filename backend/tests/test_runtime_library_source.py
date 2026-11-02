from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.runtime_library_source import (
    RUNTIME_LIBRARY_SOURCE_RELATIVE_PATH,
    configure_runtime_library_source,
    inspect_runtime_library_root,
)


def _elf(machine: int, label: str) -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header) + label.encode("utf-8")


class RuntimeLibrarySourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_configuration_records_one_root_and_no_library_copies(self) -> None:
        library_root = self.root / "runtime-libraries"
        library_root.mkdir()
        (library_root / "libEGL.so").write_bytes(_elf(183, "egl"))
        (library_root / "libGLESv3.so").write_bytes(_elf(183, "gles"))
        (library_root / "libc.so").write_bytes(_elf(183, "libc"))
        state_root = self.root / "state"

        result = configure_runtime_library_source(library_root=library_root, state_root=state_root)

        registry_path = state_root / RUNTIME_LIBRARY_SOURCE_RELATIVE_PATH
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "configured")
        self.assertEqual(registry["library_root"], str(library_root.resolve()))
        self.assertEqual(
            {item["name"] for item in registry["libraries"]},
            {"libEGL.so", "libGLESv3.so", "libc.so"},
        )
        self.assertFalse((state_root / "build-worker" / "profile-runtime-libraries").exists())
        gles = next(item for item in registry["profiles"] if item["profile_id"] == "gpu_gles3")
        self.assertEqual(gles["status"], "ready")

    def test_inspection_reports_partial_profile_availability(self) -> None:
        library_root = self.root / "runtime-libraries"
        library_root.mkdir()
        (library_root / "libvulkan.so").write_bytes(_elf(183, "vulkan"))

        inspection = inspect_runtime_library_root(library_root)

        vulkan = next(item for item in inspection["profiles"] if item["profile_id"] == "gpu_vulkan")
        self.assertEqual(vulkan["status"], "missing_libraries")
        self.assertEqual(vulkan["missing_library_names"], ["libc.so"])

    def test_empty_unknown_directory_is_rejected(self) -> None:
        library_root = self.root / "runtime-libraries"
        library_root.mkdir()
        (library_root / "unrelated.so").write_bytes(b"unknown")

        with self.assertRaisesRegex(ValueError, "none of the known"):
            inspect_runtime_library_root(library_root)

    def test_x86_library_is_rejected_before_registration(self) -> None:
        library_root = self.root / "runtime-libraries"
        library_root.mkdir()
        (library_root / "libc.so").write_bytes(_elf(62, "x86-64"))

        with self.assertRaisesRegex(ValueError, "not AArch64"):
            inspect_runtime_library_root(library_root)


if __name__ == "__main__":
    unittest.main()
