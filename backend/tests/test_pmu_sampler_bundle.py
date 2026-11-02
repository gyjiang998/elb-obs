"""PMU asset integrity and cross-compiler selection without a phone or SDK."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import patch

from tools.scenario_capture.build_pmu_sampler import _run, build_bundle, resolve_native_sdk
from tools.scenario_capture.pmu_bundle import (
    BINARY_NAME, LEGACY_BINARY_NAME, LEGACY_SOURCE_NAME, INTERPRETER, SAMPLER_VERSION, SOURCE_PATH, TARGET,
    PmuBundleError, inspect_elf, sha256_bytes, sha256_file, verify_bundle,
)


def target_elf(*, machine: int = 183, interpreter: str = INTERPRETER) -> bytes:
    data = bytearray(256)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into("<HHIQQQIHHHHHH", data, 16, 3, machine, 1, 0x1000, 64, 0, 0, 64, 56, 2, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0x1000, 0, len(data), len(data), 4096)
    raw = interpreter.encode() + b"\0"
    struct.pack_into("<IIQQQQQQ", data, 120, 3, 4, 176, 0, 0, len(raw), len(raw), 1)
    data[176:176 + len(raw)] = raw
    return bytes(data)


def use_legacy_filenames(bundle: Path) -> tuple[Path, bytes, bytes]:
    """Reproduce the original 1.1.0 manifest/receipt/binary naming contract."""
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    version = bundle / "versions" / manifest["binary_sha256"]
    (version / BINARY_NAME).rename(version / LEGACY_BINARY_NAME)
    receipt_path = version / "receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["source"] = LEGACY_SOURCE_NAME
    receipt["command"] = [part.replace(SOURCE_PATH.name, LEGACY_SOURCE_NAME).replace(BINARY_NAME, LEGACY_BINARY_NAME)
                          for part in receipt["command"]]
    receipt_bytes = json.dumps(receipt).encode()
    receipt_path.write_bytes(receipt_bytes)
    manifest["binary"] = f"versions/{manifest['binary_sha256']}/{LEGACY_BINARY_NAME}"
    manifest["receipt_sha256"] = sha256_bytes(receipt_bytes)
    manifest_bytes = json.dumps(manifest).encode()
    manifest_path.write_bytes(manifest_bytes)
    return version, receipt_bytes, manifest_bytes


class PmuBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.bundle = self.root / "bundle"

    def tearDown(self):
        self.temporary.cleanup()

    def write_bundle(self, *, data=None, receipt_changes=None):
        data = target_elf() if data is None else data
        digest = sha256_bytes(data)
        version = self.bundle / "versions" / digest
        version.mkdir(parents=True, exist_ok=True)
        (version / BINARY_NAME).write_bytes(data)
        receipt = {
            "schema_version": "pmu_sampler_build/v1", "sampler_version": SAMPLER_VERSION,
            "target": TARGET, "source": SOURCE_PATH.name, "source_sha256": sha256_file(SOURCE_PATH),
            "compiler": {"path": "/sdk/clang", "sha256": "c" * 64, "version": "OHOS clang fixture"},
            "command": ["/sdk/clang", f"--target={TARGET}", "--sysroot=/sdk/sysroot", "-O2", "-std=c11", "-Werror"],
            "binary_sha256": digest, "binary_bytes": len(data),
        }
        receipt.update(receipt_changes or {})
        receipt_bytes = json.dumps(receipt).encode()
        (version / "receipt.json").write_bytes(receipt_bytes)
        manifest = {"schema_version": "pmu_sampler_bundle/v1", "binary_sha256": digest,
                    "binary": f"versions/{digest}/{BINARY_NAME}", "receipt": f"versions/{digest}/receipt.json",
                    "receipt_sha256": sha256_bytes(receipt_bytes)}
        (self.bundle / "manifest.json").write_text(json.dumps(manifest))
        return version, manifest

    def error(self, code):
        with self.assertRaises(PmuBundleError) as caught:
            verify_bundle(self.bundle)
        self.assertEqual(caught.exception.code, code)

    def test_valid_bundle_returns_verified_paths_and_identity_without_execution(self):
        version, _ = self.write_bundle()
        with patch("subprocess.Popen", side_effect=AssertionError("must not execute target")):
            result = verify_bundle(self.bundle)
        self.assertEqual(result["binary_path"], version / BINARY_NAME)
        self.assertEqual(result["target"], TARGET)
        self.assertEqual(result["source_sha256"], sha256_file(SOURCE_PATH))
        self.assertEqual(result["elf"]["interpreter"], INTERPRETER)

    def test_complete_legacy_bundle_remains_valid_without_target_execution(self):
        self.write_bundle()
        version, receipt_bytes, _ = use_legacy_filenames(self.bundle)
        with patch("subprocess.Popen", side_effect=AssertionError("must not execute target")):
            result = verify_bundle(self.bundle)
        self.assertEqual(result["binary_path"], version / LEGACY_BINARY_NAME)
        self.assertEqual(result["receipt_path"].read_bytes(), receipt_bytes)
        self.assertEqual(result["source_sha256"], sha256_file(SOURCE_PATH))

    def test_legacy_source_filename_does_not_relax_source_binding(self):
        self.write_bundle(receipt_changes={"source_sha256": "a" * 64})
        use_legacy_filenames(self.bundle)
        self.error("PMU_BUNDLE_SOURCE_MISMATCH")

    def test_only_canonical_or_legacy_filenames_are_accepted(self):
        self.write_bundle(receipt_changes={"source": "arbitrary_sampler.c"})
        self.error("PMU_BUNDLE_SOURCE_MISMATCH")
        _, manifest = self.write_bundle()
        manifest["binary"] = f"versions/{manifest['binary_sha256']}/arbitrary_sampler"
        (self.bundle / "manifest.json").write_text(json.dumps(manifest))
        self.error("PMU_BUNDLE_PATH_INVALID")

    def test_canonical_source_and_historical_archives_match_provenance(self):
        provenance = json.loads(SOURCE_PATH.with_name("provenance.json").read_text())
        adaptation = next(item for item in provenance["pmu_adaptations"] if item["version"] == "1.1.0")
        self.assertEqual(sha256_file(SOURCE_PATH.parent / adaptation["path"]), adaptation["sha256"])
        self.assertEqual(adaptation["sha256"], "122fb36a67ff541ca44ade51fa4803b591d2d0e2721fe0d21636cbb1bd936b76")
        current = next(item for item in provenance["pmu_adaptations"] if item["version"] == SAMPLER_VERSION)
        self.assertEqual(current["path"], SOURCE_PATH.name)
        self.assertEqual(sha256_file(SOURCE_PATH), current["sha256"])
        archived = next(item for item in provenance["files"] if item["file"] == LEGACY_SOURCE_NAME)
        self.assertEqual(sha256_file(SOURCE_PATH.parent / archived["archived_path"]), archived["sha256"])
        self.assertIn('#include "thread_pmu_sampler.c"', SOURCE_PATH.with_name(LEGACY_SOURCE_NAME).read_text())

    def test_published_provenance_and_receipts_are_portable(self):
        asset_root = SOURCE_PATH.parent
        provenance = json.loads((asset_root / "provenance.json").read_text())
        for item in provenance["files"]:
            source = item["original_path"]
            self.assertFalse(PurePosixPath(source).is_absolute(), source)
            self.assertFalse(PureWindowsPath(source).is_absolute(), source)

        bundle_root = asset_root / "prebuilt" / TARGET
        verified = verify_bundle(bundle_root)
        self.assertEqual(verified["receipt"]["compiler"]["path"], "clang")
        for receipt_path in bundle_root.glob("versions/*/receipt.json"):
            receipt = json.loads(receipt_path.read_text())
            compiler_path = receipt["compiler"]["path"]
            self.assertFalse(PurePosixPath(compiler_path).is_absolute(), compiler_path)
            self.assertFalse(PureWindowsPath(compiler_path).is_absolute(), compiler_path)
            installed_dir = next(
                line for line in receipt["compiler"]["version"].splitlines()
                if line.startswith("InstalledDir: ")
            )
            self.assertEqual(installed_dir, "InstalledDir: <OPENHARMONY_NATIVE_SDK>/llvm/bin")
            self.assertEqual(receipt["command"][0], receipt["compiler"]["path"])
            self.assertEqual(receipt["command"][-1], BINARY_NAME)
            self.assertEqual(receipt["command"][-3], SOURCE_PATH.name)
            self.assertEqual(receipt["command"][2], "--sysroot=<OPENHARMONY_NATIVE_SDK>/sysroot")

    def test_missing_bundle_explains_unavailable(self):
        self.error("PMU_BUNDLE_UNAVAILABLE")

    def test_binary_mutation_is_detected(self):
        version, _ = self.write_bundle()
        binary = version / BINARY_NAME
        binary.write_bytes(binary.read_bytes() + b"tamper")
        self.error("PMU_BUNDLE_HASH_MISMATCH")

    def test_receipt_mutation_is_detected_before_reading_fields(self):
        version, _ = self.write_bundle()
        (version / "receipt.json").write_text('{}')
        self.error("PMU_BUNDLE_HASH_MISMATCH")

    def test_stale_source_is_rejected_even_with_consistent_receipt_hash(self):
        self.write_bundle(receipt_changes={"source_sha256": "a" * 64})
        self.error("PMU_BUNDLE_SOURCE_MISMATCH")

    def test_wrong_sampler_version_or_target_is_rejected(self):
        for changes in ({"sampler_version": "0.0.1"}, {"target": "x86_64-linux-gnu"}):
            with self.subTest(changes=changes):
                self.write_bundle(receipt_changes=changes)
                self.error("PMU_BUNDLE_MANIFEST_INVALID")

    def test_host_elf_and_wrong_loader_rejected_with_consistent_hashes(self):
        for data in (b"Mach-O binary", target_elf(machine=62), target_elf(interpreter="/lib64/ld-linux-x86-64.so.2")):
            with self.subTest(data=data[:20]):
                self.write_bundle(data=data)
                self.error("PMU_BUNDLE_ELF_INVALID")

    def test_malformed_program_headers_rejected(self):
        data = bytearray(target_elf())
        struct.pack_into("<Q", data, 32, len(data) + 1)
        self.write_bundle(data=bytes(data))
        self.error("PMU_BUNDLE_ELF_INVALID")

    def test_manifest_path_traversal_is_rejected(self):
        _, manifest = self.write_bundle()
        manifest["binary"] = "../outside"
        (self.bundle / "manifest.json").write_text(json.dumps(manifest))
        self.error("PMU_BUNDLE_PATH_INVALID")

    def test_binary_and_version_directory_symlink_escape_rejected(self):
        version, _ = self.write_bundle()
        binary = version / BINARY_NAME
        outside = self.root / "outside"
        outside.write_bytes(binary.read_bytes())
        binary.unlink()
        binary.symlink_to(outside)
        self.error("PMU_BUNDLE_PATH_INVALID")
        binary.unlink()
        binary.write_bytes(outside.read_bytes())
        external_version = self.root / "external-version"
        version.rename(external_version)
        version.symlink_to(external_version, target_is_directory=True)
        self.error("PMU_BUNDLE_PATH_INVALID")

    def test_missing_target_flag_in_build_receipt_is_rejected(self):
        self.write_bundle(receipt_changes={"command": ["/sdk/clang", "-O2", "-std=c11", "-Werror", "--sysroot=/sdk/sysroot"]})
        self.error("PMU_BUNDLE_MANIFEST_INVALID")


class PmuBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.sdk = self.root / "sdk" / "native"
        self.compiler = self.sdk / "llvm" / "bin" / ("clang.exe" if os.name == "nt" else "clang")
        self.compiler.parent.mkdir(parents=True)
        self.compiler.write_text("compiler fixture")
        self.compiler.chmod(0o755)
        for relative in ("usr/include/linux/perf_event.h", f"usr/lib/{TARGET}/Scrt1.o", f"usr/lib/{TARGET}/libc.so"):
            path = self.sdk / "sysroot" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        self.bundle = self.root / "bundle"

    def tearDown(self):
        self.temporary.cleanup()

    def compiler_fixture(self, command, *, timeout):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(
                command, 0, f"OHOS clang fixture\nInstalledDir: {self.sdk / 'llvm' / 'bin'}", "",
            )
        self.assertEqual(timeout, 120)
        self.assertEqual(command[0], str(self.compiler))
        self.assertIn(f"--target={TARGET}", command)
        self.assertIn(str(SOURCE_PATH), command)
        self.assertIn(f"--sysroot={self.sdk / 'sysroot'}", command)
        Path(command[command.index("-o") + 1]).write_bytes(target_elf())
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_sdk_accepts_native_root_parent_and_dedicated_environment(self):
        for path in (self.sdk, self.sdk.parent):
            self.assertEqual(resolve_native_sdk(path), (self.sdk, self.compiler))
        with patch.dict(os.environ, {"HETERO_OPENHARMONY_NATIVE_SDK_ROOT": str(self.sdk)}):
            self.assertEqual(resolve_native_sdk(), (self.sdk, self.compiler))

    def test_missing_sdk_and_compact_headers_never_fall_back_to_host_compiler(self):
        with patch.dict(os.environ, {"HETERO_OPENHARMONY_NATIVE_SDK_ROOT": "", "HETERO_OPENHARMONY_SDK_ROOT": str(self.sdk)}):
            with self.assertRaises(PmuBundleError) as caught:
                resolve_native_sdk()
            self.assertEqual(caught.exception.code, "PMU_NATIVE_SDK_UNAVAILABLE")
        (self.sdk / "sysroot" / "usr" / "lib" / TARGET / "libc.so").unlink()
        with self.assertRaises(PmuBundleError) as caught:
            build_bundle(native_sdk=self.sdk, output=self.bundle)
        self.assertEqual(caught.exception.code, "PMU_NATIVE_SDK_UNAVAILABLE")
        self.assertFalse(self.bundle.exists())

    def test_fixed_build_publishes_verified_receipt_and_reuses_identical_version(self):
        with patch("tools.scenario_capture.build_pmu_sampler._run", side_effect=self.compiler_fixture):
            result = build_bundle(native_sdk=self.sdk, output=self.bundle)
            original_receipt = result["receipt_path"].read_bytes()
            again = build_bundle(native_sdk=self.sdk, output=self.bundle)
        self.assertEqual(result["sha256"], again["sha256"])
        self.assertEqual(again["receipt_path"].read_bytes(), original_receipt)
        self.assertFalse(list(self.bundle.glob(".pmu-build-*")))
        receipt = json.loads(original_receipt)
        self.assertEqual(receipt["compiler"]["sha256"], sha256_file(self.compiler))
        self.assertEqual(receipt["compiler"]["path"], self.compiler.name)
        self.assertEqual(receipt["command"], [
            self.compiler.name, f"--target={TARGET}", "--sysroot=<OPENHARMONY_NATIVE_SDK>/sysroot",
            "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", SOURCE_PATH.name, "-o", BINARY_NAME,
        ])
        self.assertIn("InstalledDir: <OPENHARMONY_NATIVE_SDK>/llvm/bin", receipt["compiler"]["version"])
        self.assertNotIn(str(self.root), original_receipt.decode())

    def test_identical_build_upgrades_legacy_filename_without_replacing_original_files(self):
        with patch("tools.scenario_capture.build_pmu_sampler._run", side_effect=self.compiler_fixture):
            original = build_bundle(native_sdk=self.sdk, output=self.bundle)
            version, receipt_bytes, legacy_manifest = use_legacy_filenames(self.bundle)
            original_binary = (version / LEGACY_BINARY_NAME).read_bytes()
            result = build_bundle(native_sdk=self.sdk, output=self.bundle)
            repeated = build_bundle(native_sdk=self.sdk, output=self.bundle)
        self.assertEqual(result["sha256"], original["sha256"])
        self.assertEqual(result["binary_path"], version / BINARY_NAME)
        self.assertEqual(result["receipt_path"].read_bytes(), receipt_bytes)
        self.assertEqual((version / LEGACY_BINARY_NAME).read_bytes(), original_binary)
        self.assertEqual(result["binary_path"].read_bytes(), original_binary)
        self.assertEqual(repeated["receipt_path"].read_bytes(), receipt_bytes)
        # The previous manifest remains usable against the untouched legacy
        # files, including after another canonical build.
        (self.bundle / "manifest.json").write_bytes(legacy_manifest)
        self.assertEqual(verify_bundle(self.bundle)["binary_path"], version / LEGACY_BINARY_NAME)

    def test_invalid_legacy_receipt_does_not_add_binary_or_replace_manifest(self):
        with patch("tools.scenario_capture.build_pmu_sampler._run", side_effect=self.compiler_fixture):
            build_bundle(native_sdk=self.sdk, output=self.bundle)
            version, _, _ = use_legacy_filenames(self.bundle)
            receipt = json.loads((version / "receipt.json").read_bytes())
            receipt["source_sha256"] = "a" * 64
            receipt_bytes = json.dumps(receipt).encode()
            (version / "receipt.json").write_bytes(receipt_bytes)
            manifest_path = self.bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            manifest["receipt_sha256"] = sha256_bytes(receipt_bytes)
            manifest_bytes = json.dumps(manifest).encode()
            manifest_path.write_bytes(manifest_bytes)
            with self.assertRaises(PmuBundleError) as caught:
                build_bundle(native_sdk=self.sdk, output=self.bundle)
        self.assertEqual(caught.exception.code, "PMU_BUNDLE_SOURCE_MISMATCH")
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
        self.assertFalse((version / BINARY_NAME).exists())
        self.assertEqual((version / "receipt.json").read_bytes(), receipt_bytes)
        self.assertEqual((version / LEGACY_BINARY_NAME).read_bytes(), target_elf())

    def test_invalid_compile_output_preserves_previous_bundle(self):
        with patch("tools.scenario_capture.build_pmu_sampler._run", side_effect=self.compiler_fixture):
            before = build_bundle(native_sdk=self.sdk, output=self.bundle)
        manifest_bytes = before["manifest_path"].read_bytes()

        def host_output(command, *, timeout):
            result = self.compiler_fixture(command, timeout=timeout)
            if "-o" in command:
                Path(command[command.index("-o") + 1]).write_bytes(target_elf(machine=62))
            return result

        with patch("tools.scenario_capture.build_pmu_sampler._run", side_effect=host_output):
            with self.assertRaises(PmuBundleError) as caught:
                build_bundle(native_sdk=self.sdk, output=self.bundle)
        self.assertEqual(caught.exception.code, "PMU_BUNDLE_ELF_INVALID")
        self.assertEqual(before["manifest_path"].read_bytes(), manifest_bytes)
        self.assertEqual(verify_bundle(self.bundle)["sha256"], before["sha256"])

    def test_compile_failure_preserves_prior_manifest(self):
        with patch("tools.scenario_capture.build_pmu_sampler._run", side_effect=self.compiler_fixture):
            before = build_bundle(native_sdk=self.sdk, output=self.bundle)
        with patch("tools.scenario_capture.build_pmu_sampler._run", side_effect=PmuBundleError("PMU_BUILD_TIMEOUT", "timeout")):
            with self.assertRaises(PmuBundleError):
                build_bundle(native_sdk=self.sdk, output=self.bundle)
        self.assertEqual(verify_bundle(self.bundle)["sha256"], before["sha256"])

    def test_timeout_reaps_owned_compiler_process(self):
        spawned = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        with patch("tools.scenario_capture.build_pmu_sampler.subprocess.Popen", side_effect=launch):
            with self.assertRaises(PmuBundleError) as caught:
                _run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
        self.assertEqual(caught.exception.code, "PMU_BUILD_TIMEOUT")
        self.assertIsNotNone(spawned[0].poll())


if __name__ == "__main__":
    unittest.main()
