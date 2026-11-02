"""Repository PMU bundle provisioning without an SDK, compiler, or device."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.tests.test_pmu_sampler_bundle import target_elf, use_legacy_filenames
from tools.scenario_capture.pmu_bundle import (
    BINARY_NAME, SAMPLER_VERSION, SOURCE_PATH, TARGET, PmuBundleError,
    sha256_bytes, sha256_file, verify_bundle,
)
from tools.scenario_capture.provision_pmu_sampler import BUNDLED_ROOT, main, provision_bundle


def write_bundle(root: Path, *, data: bytes | None = None, source_hash: str | None = None) -> Path:
    data = target_elf() if data is None else data
    digest = sha256_bytes(data)
    version = root / "versions" / digest
    version.mkdir(parents=True)
    (version / BINARY_NAME).write_bytes(data)
    receipt = {
        "schema_version": "pmu_sampler_build/v1", "sampler_version": SAMPLER_VERSION,
        "target": TARGET, "source": SOURCE_PATH.name,
        "source_sha256": source_hash or sha256_file(SOURCE_PATH),
        "compiler": {"path": "/sdk/clang", "sha256": "c" * 64, "version": "fixture"},
        "command": ["/sdk/clang", f"--target={TARGET}", "--sysroot=/sdk/sysroot", "-std=c11", "-O2", "-Werror"],
        "binary_sha256": digest, "binary_bytes": len(data),
    }
    receipt_bytes = json.dumps(receipt).encode()
    (version / "receipt.json").write_bytes(receipt_bytes)
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": "pmu_sampler_bundle/v1", "binary_sha256": digest,
        "binary": f"versions/{digest}/{BINARY_NAME}", "receipt": f"versions/{digest}/receipt.json",
        "receipt_sha256": sha256_bytes(receipt_bytes),
    }))
    return version


class PmuSamplerProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "repository" / "prebuilt"
        self.output = self.root / "runtime" / "assets" / "pmu-sampler"
        self.source_version = write_bundle(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def error(self, code: str):
        with self.assertRaises(PmuBundleError) as caught:
            provision_bundle(self.output, self.source)
        self.assertEqual(caught.exception.code, code)

    def test_repository_bundle_is_bound_to_current_source_and_installs_without_sdk(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no compiler or target execution")):
            checked = verify_bundle(BUNDLED_ROOT)
            result = provision_bundle(self.output)
        self.assertEqual(result["status"], "installed")
        self.assertEqual(result["binary_sha256"], checked["binary_sha256"])
        self.assertEqual(result["source_sha256"], sha256_file(SOURCE_PATH))
        self.assertEqual(result["target"], TARGET)

    def test_first_install_without_sdk_compiler_or_target_execution(self):
        with patch.dict(os.environ, {"HETERO_OPENHARMONY_NATIVE_SDK_ROOT": ""}), patch("subprocess.Popen", side_effect=AssertionError("no execution")):
            result = provision_bundle(self.output, self.source)
        self.assertEqual(result["status"], "installed")
        self.assertEqual(result["binary_sha256"], verify_bundle(self.output)["binary_sha256"])
        self.assertEqual(result["target"], TARGET)
        json.dumps(result)
        self.assertFalse(list(self.output.glob(".pmu-provision-*")))

    def test_repeated_install_is_noop_even_if_repository_source_unavailable(self):
        provision_bundle(self.output, self.source)
        manifest = self.output / "manifest.json"
        before = manifest.stat()
        result = provision_bundle(self.output, self.root / "missing-source")
        self.assertEqual(result["status"], "reused")
        self.assertEqual(manifest.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(manifest.stat().st_ino, before.st_ino)

    def test_legacy_bundle_is_reused_without_renaming_or_rewriting(self):
        write_bundle(self.output)
        version, receipt, manifest = use_legacy_filenames(self.output)
        before = sorted(p.name for p in version.iterdir())
        result = provision_bundle(self.output, self.source)
        self.assertEqual(result["status"], "reused")
        self.assertEqual((self.output / "manifest.json").read_bytes(), manifest)
        self.assertEqual((version / "receipt.json").read_bytes(), receipt)
        self.assertEqual(sorted(p.name for p in version.iterdir()), before)

    def test_stale_source_activates_new_version_and_preserves_old_version(self):
        old_data = target_elf()[:-1] + b"x"
        old_version = write_bundle(self.output, data=old_data, source_hash="a" * 64)
        old_receipt = (old_version / "receipt.json").read_bytes()
        result = provision_bundle(self.output, self.source)
        self.assertEqual(result["status"], "installed")
        self.assertEqual((old_version / BINARY_NAME).read_bytes(), old_data)
        self.assertEqual((old_version / "receipt.json").read_bytes(), old_receipt)
        self.assertEqual(len(list((self.output / "versions").iterdir())), 2)
        verify_bundle(self.output)

    def test_invalid_source_leaves_existing_manifest_and_files_untouched(self):
        write_bundle(self.output, data=target_elf()[:-1] + b"x", source_hash="a" * 64)
        before = {str(p.relative_to(self.output)): p.read_bytes() for p in self.output.rglob("*") if p.is_file()}
        (self.source_version / BINARY_NAME).write_bytes(b"invalid")
        self.error("PMU_BUNDLE_HASH_MISMATCH")
        after = {str(p.relative_to(self.output)): p.read_bytes() for p in self.output.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_invalid_source_does_not_create_destination(self):
        (self.source / "manifest.json").write_text("invalid")
        self.error("PMU_BUNDLE_MANIFEST_INVALID")
        self.assertFalse(self.output.exists())

    def test_corrupt_same_hash_version_is_not_overwritten(self):
        version = write_bundle(self.output)
        binary = version / BINARY_NAME
        binary.write_bytes(b"damaged installed binary")
        manifest = (self.output / "manifest.json").read_bytes()
        self.error("PMU_PROVISION_VERSION_CONFLICT")
        self.assertEqual(binary.read_bytes(), b"damaged installed binary")
        self.assertEqual((self.output / "manifest.json").read_bytes(), manifest)

    def test_version_appearing_during_staging_cannot_replace_active_manifest(self):
        write_bundle(self.output, data=target_elf()[:-1] + b"x", source_hash="a" * 64)
        active_manifest = (self.output / "manifest.json").read_bytes()
        target = self.output / "versions" / self.source_version.name
        real_temporary_directory = tempfile.TemporaryDirectory
        def competing_install(*args, **kwargs):
            if kwargs.get("prefix") == ".pmu-provision-":
                target.mkdir()
                (target / BINARY_NAME).write_bytes(b"unverified competing version")
            return real_temporary_directory(*args, **kwargs)
        with patch("tools.scenario_capture.provision_pmu_sampler.tempfile.TemporaryDirectory", side_effect=competing_install):
            self.error("PMU_PROVISION_VERSION_CONFLICT")
        self.assertEqual((self.output / "manifest.json").read_bytes(), active_manifest)
        self.assertEqual((target / BINARY_NAME).read_bytes(), b"unverified competing version")

    def test_missing_manifest_can_reactivate_valid_immutable_version(self):
        version = write_bundle(self.output)
        version, receipt, _ = use_legacy_filenames(self.output)
        (self.output / "manifest.json").unlink()
        before = sorted(p.name for p in version.iterdir())
        result = provision_bundle(self.output, self.source)
        self.assertEqual(result["status"], "installed")
        self.assertEqual(sorted(p.name for p in version.iterdir()), before)
        self.assertEqual((version / "receipt.json").read_bytes(), receipt)
        verify_bundle(self.output)

    def test_output_root_and_managed_paths_reject_symlinks(self):
        for relative in ("", "versions", "manifest.json"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(dir=self.root) as case:
                base = Path(case)
                output = base / "output"
                outside = base / "outside"
                outside.mkdir()
                if relative:
                    output.mkdir()
                (output / relative if relative else output).symlink_to(outside, target_is_directory=True)
                with self.assertRaises(PmuBundleError) as caught:
                    provision_bundle(output, self.source)
                self.assertEqual(caught.exception.code, "PMU_BUNDLE_PATH_INVALID")
                self.assertEqual(list(outside.iterdir()), [])

    def test_hash_directory_symlink_is_rejected_without_writing_outside(self):
        versions = self.output / "versions"
        versions.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (versions / self.source_version.name).symlink_to(outside, target_is_directory=True)
        self.error("PMU_BUNDLE_PATH_INVALID")
        self.assertEqual(list(outside.iterdir()), [])

    def test_non_bundle_directory_is_not_used_as_install_target(self):
        self.output.mkdir(parents=True)
        (self.output / "user-data.txt").write_text("keep")
        self.error("PMU_BUNDLE_PATH_INVALID")
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["user-data.txt"])

    def test_unrelated_manifest_is_not_overwritten(self):
        self.output.mkdir(parents=True)
        manifest = self.output / "manifest.json"
        manifest.write_text('{"name":"another-application"}')
        self.error("PMU_BUNDLE_PATH_INVALID")
        self.assertEqual(manifest.read_text(), '{"name":"another-application"}')
        self.assertFalse((self.output / "versions").exists())

    def test_symlinked_source_root_is_rejected(self):
        link = self.root / "linked-source"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(PmuBundleError) as caught:
            provision_bundle(self.output, link)
        self.assertEqual(caught.exception.code, "PMU_BUNDLE_PATH_INVALID")
        self.assertFalse(self.output.exists())

    def test_snapshot_revalidates_manifest_before_constructing_paths(self):
        from tools.scenario_capture import provision_pmu_sampler as provision
        original_read = provision._read_bytes
        def changed(path, limit):
            if path.name == "manifest.json":
                return b'{"binary_sha256":"../../escape","binary":"../../escape"}'
            return original_read(path, limit)
        with patch.object(provision, "_read_bytes", side_effect=changed):
            self.error("PMU_BUNDLE_PATH_INVALID")
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / "escape").exists())

    def test_snapshot_manifest_requires_json_object(self):
        from tools.scenario_capture import provision_pmu_sampler as provision
        original_read = provision._read_bytes
        for data in (b"null", b"[]", b"[1]", b'"manifest"', b"42"):
            with self.subTest(manifest=data):
                def changed(path, limit):
                    return data if path.name == "manifest.json" else original_read(path, limit)
                with patch.object(provision, "_read_bytes", side_effect=changed):
                    self.error("PMU_BUNDLE_PATH_INVALID")
                self.assertFalse(self.output.exists())

    def test_cli_respects_output_environment_and_returns_small_json(self):
        result = {"status": "installed", "target": TARGET}
        stream = io.StringIO()
        with patch.dict(os.environ, {"HETERO_PMU_SAMPLER_BUNDLE_ROOT": str(self.output)}), patch("sys.argv", ["provision"]), patch("tools.scenario_capture.provision_pmu_sampler.provision_bundle", return_value=result) as installer, contextlib.redirect_stdout(stream):
            self.assertEqual(main(), 0)
        installer.assert_called_once_with(self.output)
        self.assertEqual(json.loads(stream.getvalue()), result)


if __name__ == "__main__":
    unittest.main()
