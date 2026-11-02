from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.build_worker as build_worker_module
from backend.build_worker import BuildWorker
from backend.core import ArtifactStore, PlatformError
from backend.device_experiment import EXPERIMENT_PROFILES, EXPERIMENT_ROOT
from backend.runtime_library_source import configure_runtime_library_source


def _aarch64_elf(label: str) -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[18:20] = (183).to_bytes(2, "little")
    return bytes(header) + label.encode("utf-8")


class BuildWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.state_root = self.root / "state"
        self.store = ArtifactStore(self.state_root)
        self.worker = BuildWorker(workspace_root=self.root, state_root=self.state_root, artifact_store=self.store)

    def _source_artifact(self, profile_id: str = "native_c") -> dict[str, object]:
        source = b"void _start(void) {}\n"
        digest = hashlib.sha256(source).hexdigest()
        relative = (EXPERIMENT_ROOT / "sources" / "test-source.c").as_posix()
        path = self.state_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source)
        return self.store.write(
            "build_source",
            {
                "schema_version": "build_source/v1",
                "agent_run_id": "run_aaaaaaaaaaaaaaaaaaaaaaaa",
                "profile_id": profile_id,
                "language": "c",
                "source_relative_path": relative,
                "source_sha256": digest,
                "source_bytes": len(source),
            },
            source_connector_id="connector_test",
            source_binding={"match_level": "unknown"},
            producer={"name": "test", "version": "1"},
        )

    @staticmethod
    def _fake_compile(
        workspace_root: Path,
        state_root: Path,
        run_root: Path,
        source: Path,
        profile_id: str,
        profile: dict[str, object],
        local_libraries: list[Path],
        **_: object,
    ) -> tuple[Path, dict[str, object]]:
        del workspace_root, state_root, source, profile_id, profile, local_libraries
        binary = run_root / "experiment.bin"
        binary.write_bytes(b"ELF-test-binary")
        return binary, {
            "compile": {"return_code": 0, "stdout": "compile ok", "stderr": ""},
            "link": {"return_code": 0, "stdout": "", "stderr": "link ok"},
        }

    def test_e0_build_materializes_manifest_package_and_cache(self) -> None:
        source = self._source_artifact()
        toolchain = {"status": "available", "files": {"clang": {"sha256": "a" * 64}}}

        with patch.object(self.worker, "_toolchain_identity", return_value=toolchain), patch("backend.build_worker._compile_and_link", side_effect=self._fake_compile):
            first = self.worker.build(
                project_id="local-default",
                agent_run_id="run_aaaaaaaaaaaaaaaaaaaaaaaa",
                source_artifact=source,
                profile_id="native_c",
                target={"os": "OpenHarmony", "abi": "arm64"},
            )
            second = self.worker.build(
                project_id="local-default",
                agent_run_id="run_aaaaaaaaaaaaaaaaaaaaaaaa",
                source_artifact=source,
                profile_id="native_c",
                target={"os": "OpenHarmony", "abi": "arm64"},
            )

        self.assertEqual(first["status"], "succeeded")
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["build_identity"], second["build_identity"])
        bundle = self.state_root / str(first["bundle_relative_path"])
        self.assertTrue((bundle / "manifest.json").is_file())
        self.assertTrue((bundle / "bin" / "experiment.bin").is_file())
        self.assertTrue((bundle / "run" / "experiment.json").is_file())
        self.assertEqual((bundle / "logs" / "build.stdout").read_text(encoding="utf-8"), "[compile]\ncompile ok\n")
        self.assertEqual((bundle / "logs" / "build.stderr").read_text(encoding="utf-8"), "[link]\nlink ok\n")
        self.assertEqual(self.worker.binary_path(first), (bundle / "bin" / "experiment.bin").resolve())
        resource_policy = first["manifest"]["resource_policy"]
        self.assertEqual(resource_policy["id"], "build-worker-default/v3")
        self.assertIsNone(resource_policy["max_processes"])
        self.assertEqual(resource_policy["process_limit_enforcement"], "not_applied_shared_uid")
        self.assertEqual(resource_policy["linker_threads"], 1)

    def test_profile_description_comes_from_the_same_fixed_build_surface(self) -> None:
        profiles = self.worker.describe_profile(profile_id="native_c", header_query="stdint")

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["profile_id"], "native_c")
        self.assertEqual(profiles[0]["unit"], "native")
        self.assertIn("runtime_contract_zh", profiles[0])
        self.assertTrue(profiles[0]["runtime_library_surface"]["build_create_ready"])

    def test_profile_description_exposes_missing_dynamic_runtime_before_build(self) -> None:
        with patch("backend.build_worker.experiment_profile_catalog", return_value=[{"profile_id": "gpu_gles3"}]):
            profiles = self.worker.describe_profile(profile_id="gpu_gles3")

        runtime = profiles[0]["runtime_library_surface"]
        self.assertFalse(runtime["build_create_ready"])
        self.assertEqual(runtime["error"]["code"], "BUILD_PROFILE_RUNTIME_UNAVAILABLE")
        self.assertCountEqual(
            runtime["error"]["details"]["missing_library_names"],
            ["libEGL.so", "libGLESv3.so", "libc.so"],
        )

    def test_failed_build_returns_actionable_runtime_diagnostic(self) -> None:
        source = self._source_artifact("gpu_gles3")
        toolchain = {"status": "available", "files": {"clang": {"sha256": "a" * 64}}}

        with patch.object(self.worker, "_toolchain_identity", return_value=toolchain):
            result = self.worker.build(
                project_id="local-default",
                agent_run_id="run_aaaaaaaaaaaaaaaaaaaaaaaa",
                source_artifact=source,
                profile_id="gpu_gles3",
                target={"os": "OpenHarmony", "abi": "arm64"},
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["diagnostic"]["stage"], "profile_runtime")
        self.assertEqual(result["diagnostic"]["error"]["code"], "BUILD_PROFILE_RUNTIME_UNAVAILABLE")
        self.assertIn("BUILD_PROFILE_RUNTIME_UNAVAILABLE", result["summary_zh"])

    def test_compile_failure_gets_stable_error_code_and_stderr(self) -> None:
        diagnostic = self.worker._build_diagnostic({
            "status": "compile_failed",
            "logs": {"compile": {"return_code": 1, "stderr": "source.c:2: error: expected identifier"}},
        })

        self.assertEqual(diagnostic["stage"], "compile")
        self.assertEqual(diagnostic["error"]["code"], "BUILD_COMPILE_FAILED")
        self.assertIn("expected identifier", diagnostic["error"]["message"])

    def test_linker_thread_exhaustion_is_classified_as_worker_resource_failure(self) -> None:
        diagnostic = self.worker._build_diagnostic({
            "status": "compile_failed",
            "logs": {
                "compile": {"return_code": 0, "stderr": ""},
                "link": {
                    "return_code": -6,
                    "stderr": (
                        "libc++abi: terminating due to uncaught exception of type "
                        "std::__1::system_error: thread constructor failed: Resource temporarily unavailable"
                    ),
                },
            },
        })

        self.assertEqual(diagnostic["stage"], "link")
        self.assertEqual(diagnostic["error"]["code"], "BUILD_LINK_RESOURCE_UNAVAILABLE")
        self.assertFalse(diagnostic["error"]["details"]["source_rewrite_recommended"])
        self.assertEqual(
            diagnostic["error"]["details"]["remediation_kind"],
            "build_worker_host_resource",
        )

    def test_preexec_limits_do_not_apply_shared_uid_process_limit(self) -> None:
        resource_module = build_worker_module.resource
        if resource_module is None:
            self.skipTest("POSIX resource module is unavailable")

        with patch.object(resource_module, "setrlimit") as setrlimit:
            self.worker._preexec_limits()

        applied_limits = [call.args[0] for call in setrlimit.call_args_list]
        self.assertNotIn(resource_module.RLIMIT_NPROC, applied_limits)
        self.assertIn(resource_module.RLIMIT_CPU, applied_limits)
        self.assertIn(resource_module.RLIMIT_AS, applied_limits)

    def test_dynamic_profile_bundle_requires_hash_pinned_manifest(self) -> None:
        profile_id = "gpu_gles3"
        bundle_root = self.state_root / "build-worker" / "profile-runtime-libraries" / profile_id
        bundle_root.mkdir(parents=True)
        libraries = []
        for _, name in EXPERIMENT_PROFILES[profile_id]["libraries"]:
            path = bundle_root / name
            path.write_bytes(f"fixed-{name}".encode("utf-8"))
            libraries.append({"name": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        (bundle_root / "bundle.json").write_text(json.dumps({
            "schema_version": "build_profile_runtime_bundle/v1",
            "profile_id": profile_id,
            "libraries": libraries,
        }), encoding="utf-8")

        paths, manifest = self.worker._fixed_profile_libraries(profile_id)

        self.assertEqual([path.name for path in paths], [name for _, name in EXPERIMENT_PROFILES[profile_id]["libraries"]])
        self.assertEqual(manifest["source"], "legacy_fixed_profile_bundle")
        (bundle_root / paths[0].name).write_bytes(b"tampered")
        with self.assertRaisesRegex(PlatformError, "哈希"):
            self.worker._fixed_profile_libraries(profile_id)

    def test_shared_runtime_root_serves_profile_without_profile_directory(self) -> None:
        library_root = self.root / "all-aarch64-libraries"
        library_root.mkdir()
        profile_id = "gpu_gles3"
        for _, name in EXPERIMENT_PROFILES[profile_id]["libraries"]:
            (library_root / name).write_bytes(_aarch64_elf(f"shared-{name}"))
        configured = configure_runtime_library_source(
            library_root=library_root,
            state_root=self.state_root,
        )

        paths, manifest = self.worker._fixed_profile_libraries(profile_id)

        self.assertEqual(manifest["source"], "administrator_shared_runtime_root")
        self.assertEqual(manifest["configuration_source"], "registry")
        self.assertEqual(Path(manifest["root"]), library_root.resolve())
        self.assertEqual([path.parent for path in paths], [library_root.resolve()] * len(paths))
        self.assertFalse((self.state_root / "build-worker" / "profile-runtime-libraries").exists())
        self.assertTrue(Path(configured["registry_path"]).is_file())

    def test_shared_runtime_registry_rejects_changed_library(self) -> None:
        library_root = self.root / "all-aarch64-libraries"
        library_root.mkdir()
        profile_id = "gpu_vulkan"
        for _, name in EXPERIMENT_PROFILES[profile_id]["libraries"]:
            (library_root / name).write_bytes(_aarch64_elf(f"shared-{name}"))
        configure_runtime_library_source(library_root=library_root, state_root=self.state_root)
        (library_root / "libvulkan.so").write_bytes(_aarch64_elf("changed-after-registration"))

        with self.assertRaisesRegex(PlatformError, "哈希"):
            self.worker._fixed_profile_libraries(profile_id)

    def test_environment_shared_runtime_root_needs_no_registry(self) -> None:
        library_root = self.root / "environment-libraries"
        library_root.mkdir()
        profile_id = "gpu_vulkan"
        for _, name in EXPERIMENT_PROFILES[profile_id]["libraries"]:
            (library_root / name).write_bytes(_aarch64_elf(f"environment-{name}"))

        with patch.dict(os.environ, {"HETERO_BUILD_RUNTIME_LIBRARY_ROOT": str(library_root)}, clear=False):
            _, manifest = self.worker._fixed_profile_libraries(profile_id)

        self.assertEqual(manifest["source"], "administrator_shared_runtime_root")
        self.assertEqual(manifest["configuration_source"], "environment")
        self.assertIsNone(manifest["registry_path"])

    def test_runtime_snapshot_is_hash_checked_before_linking(self) -> None:
        source_root = self.root / "source-libraries"
        source_root.mkdir()
        source = source_root / "libc.so"
        source.write_bytes(b"registered-libc")
        expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        run_root = self.state_root / "build-worker" / "cache" / "test-run"
        run_root.mkdir(parents=True)

        snapshots, members = self.worker._snapshot_runtime_libraries(
            run_root,
            [source],
            {"libraries": [{"name": "libc.so", "sha256": expected_hash}]},
        )

        self.assertEqual(snapshots, [run_root / "lib" / "libc.so"])
        self.assertEqual(members[0]["sha256"], expected_hash)
        source.write_bytes(b"changed-later")
        self.assertEqual((run_root / "lib" / "libc.so").read_bytes(), b"registered-libc")

    def test_required_network_sandbox_fails_closed_without_admin_wrapper(self) -> None:
        with patch.dict(os.environ, {
            "HETERO_BUILD_REQUIRE_NETWORK_SANDBOX": "1",
            "HETERO_BUILD_SANDBOX_PREFIX": "",
            "HETERO_BUILD_SANDBOX_NETWORK_ISOLATED": "",
        }, clear=False):
            with self.assertRaisesRegex(PlatformError, "网络隔离"):
                self.worker._network_sandbox_policy()


if __name__ == "__main__":
    unittest.main()
