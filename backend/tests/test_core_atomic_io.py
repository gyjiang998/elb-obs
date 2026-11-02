"""Atomic state writes across POSIX directory fsync and Windows CRT semantics."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend import core


class AtomicJsonWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "plans" / "plan.json"

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_windows_directory_open_is_unsupported_but_repeated_file_writes_succeed(self):
        native_open, native_fsync = os.open, os.fsync
        synchronized = []
        directory_attempts = []

        def windows_open(path, flags, *args, **kwargs):
            if Path(path).is_dir():
                directory_attempts.append(path)
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return native_open(path, flags, *args, **kwargs)

        def synchronize(fd):
            synchronized.append(stat.S_ISREG(os.fstat(fd).st_mode))
            return native_fsync(fd)

        # Replace only core's sys reference, leaving pathlib and ZIP handling on
        # the host platform. The CRT directory-open failure is real on Windows.
        with patch.object(core, "sys", SimpleNamespace(platform="win32")), \
             patch.object(core.os, "open", side_effect=windows_open), \
             patch.object(core.os, "fsync", side_effect=synchronize):
            core.atomic_write_json(self.path, {"revision": 1})
            self.assertEqual(json.loads(self.path.read_text()), {"revision": 1})
            core.atomic_write_json(self.path, {"revision": 2})

        self.assertEqual(json.loads(self.path.read_text()), {"revision": 2})
        self.assertEqual(synchronized, [True, True])
        self.assertEqual(directory_attempts, [])
        self.assert_no_temporary_files()

    @unittest.skipIf(os.name == "nt", "POSIX supports opening directory descriptors")
    def test_posix_synchronizes_both_regular_file_and_parent_directory(self):
        native_fsync = os.fsync
        synchronized = []

        def synchronize(fd):
            synchronized.append(stat.S_IFMT(os.fstat(fd).st_mode))
            return native_fsync(fd)

        with patch.object(core.os, "fsync", side_effect=synchronize):
            core.atomic_write_json(self.path, {"saved": True})
        self.assertEqual(synchronized, [stat.S_IFREG, stat.S_IFDIR])
        self.assertEqual(json.loads(self.path.read_text()), {"saved": True})

    def test_windows_file_permission_failure_remains_visible_and_keeps_existing_document(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"revision": 1}')
        with patch.object(core, "sys", SimpleNamespace(platform="win32")), \
             patch.object(core.os, "fsync", side_effect=PermissionError("file is not writable")):
            with self.assertRaisesRegex(PermissionError, "file is not writable"):
                core.atomic_write_json(self.path, {"revision": 2})
        self.assertEqual(json.loads(self.path.read_text()), {"revision": 1})
        self.assert_no_temporary_files()

    def test_windows_replace_permission_failure_remains_visible_and_keeps_existing_document(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"revision": 1}')
        with patch.object(core, "sys", SimpleNamespace(platform="win32")), \
             patch.object(core.os, "replace", side_effect=PermissionError("destination is locked")):
            with self.assertRaisesRegex(PermissionError, "destination is locked"):
                core.atomic_write_json(self.path, {"revision": 2})
        self.assertEqual(json.loads(self.path.read_text()), {"revision": 1})
        self.assert_no_temporary_files()

    def test_posix_directory_permission_failure_is_not_silently_ignored(self):
        native_open = os.open

        def denied_directory(path, flags, *args, **kwargs):
            if Path(path).is_dir():
                raise PermissionError(errno.EACCES, "directory is not readable", str(path))
            return native_open(path, flags, *args, **kwargs)

        with patch.object(core, "sys", SimpleNamespace(platform="linux")), \
             patch.object(core.os, "open", side_effect=denied_directory):
            with self.assertRaisesRegex(PermissionError, "directory is not readable"):
                core.atomic_write_json(self.path, {"revision": 1})
        # Rename completed, but the requested POSIX directory durability did not.
        self.assertEqual(json.loads(self.path.read_text()), {"revision": 1})
        self.assert_no_temporary_files()


if __name__ == "__main__":
    unittest.main()
