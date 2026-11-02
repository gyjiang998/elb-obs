from __future__ import annotations

import subprocess
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from backend import device_experiment as experiment
from backend.core import PlatformError
from backend.tests.runtime_asset_fixture import create_minimal_runtime_assets


WEB_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = WEB_ROOT


class AgentNativeDeviceExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state_root = Path(self.tempdir.name)
        llvm_bin, sdk_root = create_minimal_runtime_assets(self.state_root)
        self.sdk_root = sdk_root
        self.environment = patch.dict(
            os.environ,
            {
                "HETERO_LLVM_BIN_ROOT": str(llvm_bin),
                "HETERO_OPENHARMONY_SDK_ROOT": str(sdk_root),
                "HETERO_GLSLANG_BIN": str(self.state_root / experiment.GLSLANG_RELATIVE_PATH),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.probe = {
            "artifact_id": "art_111111111111111111111111",
            "device": {"serial": "test-device-01"},
        }

    def _stage(
        self,
        source: str = "#include <stdint.h>\nvoid _start(void) { for (;;) {} }\n",
        *,
        profile_id: str = "native_c",
        language: str = "c",
        shader_source: str = "",
    ) -> dict[str, object]:
        staged = experiment.stage_device_experiment_source(
            WORKSPACE_ROOT,
            self.state_root,
            probe_artifact=self.probe,
            profile_id=profile_id,
            title="最小实验",
            language=language,
            source=source,
            shader_source=shader_source,
        )
        staged["artifact_id"] = "art_222222222222222222222222"
        return staged

    def test_staging_complete_source_records_hash_under_artifact_root(self) -> None:
        staged = self._stage()

        self.assertEqual(staged["profile_id"], "native_c")
        self.assertEqual(staged["requested_unit"], "native")
        self.assertEqual(staged["workstation_execution_boundary"]["host_shell"], False)
        path = self.state_root / str(staged["source_relative_path"])
        self.assertTrue(path.is_file())
        self.assertEqual(experiment.sha256_file(path), staged["source_sha256"])

    def test_staging_rejects_model_selected_host_header(self) -> None:
        with self.assertRaises(PlatformError) as raised:
            self._stage('#include "/etc/passwd"\nvoid _start(void) {}\n')

        self.assertEqual(raised.exception.code, "DEVICE_EXPERIMENT_HEADER_DENIED")

    def test_gpu_profile_accepts_any_real_header_below_its_fixed_sdk_roots(self) -> None:
        staged = self._stage(
            "#include <EGL/eglext.h>\n#include <GLES3/gl31.h>\nvoid _start(void) {}\n",
            profile_id="gpu_gles3",
        )

        self.assertEqual(staged["profile_id"], "gpu_gles3")
        self.assertEqual(staged["workstation_execution_boundary"]["header_policy"], "fixed_root_resolution")

    def test_vulkan_profile_stages_host_and_glsl_as_one_immutable_input(self) -> None:
        shader = (
            "#version 450\n"
            "layout(local_size_x = 64) in;\n"
            "layout(set = 0, binding = 0) buffer Pixels { float value[]; } pixels;\n"
            "void main() { pixels.value[gl_GlobalInvocationID.x] *= 0.5; }\n"
        )
        staged = self._stage(
            "#include <vulkan/vulkan.h>\n"
            "#include \"experiment_shader_spv.h\"\n"
            "void _start(void) { (void)kExperimentShaderSpv; (void)kExperimentShaderSpvSize; }\n",
            profile_id="gpu_vulkan",
            shader_source=shader,
        )

        shader_record = staged["auxiliary_sources"]["shader"]
        self.assertEqual(staged["requested_unit"], "gpu")
        self.assertEqual(shader_record["language"], "glsl_compute")
        self.assertTrue((self.state_root / shader_record["relative_path"]).is_file())
        catalog = experiment.experiment_profile_catalog(
            WORKSPACE_ROOT,
            state_root=self.state_root,
            profile_id="gpu_vulkan",
        )[0]
        self.assertEqual(catalog["shader_contract"]["target_environment"], "vulkan1.0")
        self.assertIn("experiment_shader_spv.h", catalog["generated_headers"])

    def test_curated_hardware_api_templates_stage_against_current_profiles(self) -> None:
        template_root = WEB_ROOT / "agent-resources" / "hardware-api-templates"
        self.assertIn(
            "interface/sdk_c/graphic/graphic_2d",
            experiment.EXPERIMENT_PROFILES["media_avcodec"]["include_dirs"],
        )
        cases = (
            (
                "gpu_vulkan",
                template_root / "gpu-vulkan-compute" / "host_template.c",
                template_root / "gpu-vulkan-compute" / "kernel_template.comp",
            ),
            ("gpu_gles3", template_root / "gpu-gles3-fragment" / "host_template.c", None),
            ("gpu_gles3", template_root / "gpu-gles31-compute" / "host_template.c", None),
            ("npu_nnrt", template_root / "npu-nnrt-graph" / "host_template.c", None),
            ("media_avcodec", template_root / "media-avcodec-capability" / "host_template.c", None),
        )

        for profile_id, source_path, shader_path in cases:
            with self.subTest(profile_id=profile_id):
                staged = self._stage(
                    source_path.read_text(encoding="utf-8"),
                    profile_id=profile_id,
                    shader_source=shader_path.read_text(encoding="utf-8") if shader_path else "",
                )
                self.assertEqual(staged["profile_id"], profile_id)

    def test_vulkan_profile_rejects_host_source_without_shader(self) -> None:
        with self.assertRaises(PlatformError) as raised:
            self._stage(
                "#include <vulkan/vulkan.h>\n"
                "#include \"experiment_shader_spv.h\"\n"
                "void _start(void) {}\n",
                profile_id="gpu_vulkan",
            )

        self.assertEqual(raised.exception.code, "DEVICE_EXPERIMENT_SHADER_REQUIRED")

    def test_vulkan_shader_generation_uses_fixed_glslang_and_emits_header(self) -> None:
        staged = self._stage(
            "#include <vulkan/vulkan.h>\n"
            "#include \"experiment_shader_spv.h\"\n"
            "void _start(void) {}\n",
            profile_id="gpu_vulkan",
            shader_source="#version 450\nlayout(local_size_x = 1) in;\nvoid main() {}\n",
        )
        glslang = self.state_root / experiment.GLSLANG_RELATIVE_PATH
        glslang.parent.mkdir(parents=True, exist_ok=True)
        glslang.write_bytes(b"fixed-glslang")
        glslang.chmod(0o755)
        run_root = self.state_root / "vulkan-generation"
        run_root.mkdir()

        def run_process(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(b"\x03\x02\x23\x07" + b"\x00" * 16)
            return subprocess.CompletedProcess(command, 0, "shader ok", "")

        generated = experiment.prepare_profile_generated_sources(
            self.state_root,
            run_root,
            staged,
            experiment.EXPERIMENT_PROFILES["gpu_vulkan"],
            run_process=run_process,
        )

        header = (run_root / "experiment_shader_spv.h").read_text(encoding="ascii")
        self.assertIn("kExperimentShaderSpv", header)
        self.assertIn("kExperimentShaderSpvSize", header)
        self.assertEqual(generated["shader_compile"]["return_code"], 0)

    def test_accelerator_profiles_accept_openharmony_musl_c_headers(self) -> None:
        source = (
            "#include <unistd.h>\n"
            "#include <stdio.h>\n"
            "#include <string.h>\n"
            "#include <stdlib.h>\n"
            "void _start(void) {}\n"
        )

        for profile_id in ("gpu_gles3", "npu_nnrt", "media_avcodec"):
            with self.subTest(profile_id=profile_id):
                staged = self._stage(source, profile_id=profile_id)
                self.assertEqual(staged["profile_id"], profile_id)

        generated = self.state_root / experiment.MUSL_HEADER_OVERLAY_RELATIVE_ROOT / "bits" / "alltypes.h"
        self.assertTrue(generated.is_file())
        generated_text = generated.read_text(encoding="utf-8")
        self.assertIn("__NEED_size_t", generated_text)
        self.assertNotRegex(generated_text, r"(?m)^(?:TYPEDEF|STRUCT|UNION)\b")
        generated_syscall = self.state_root / experiment.MUSL_HEADER_OVERLAY_RELATIVE_ROOT / "bits" / "syscall.h"
        self.assertTrue(generated_syscall.is_file())
        self.assertIn("#define SYS_write 64", generated_syscall.read_text(encoding="utf-8"))

    def test_missing_musl_template_reports_deployment_recovery_instead_of_source_retry(self) -> None:
        (self.sdk_root / "third_party" / "musl" / "include" / "alltypes.h.in").unlink()

        with self.assertRaises(PlatformError) as raised:
            experiment.experiment_profile_catalog(
                WORKSPACE_ROOT,
                state_root=self.state_root,
                profile_id="gpu_gles3",
            )

        self.assertEqual(raised.exception.code, "DEVICE_EXPERIMENT_PROFILE_MISCONFIGURED")
        self.assertEqual(
            raised.exception.details["remediation_kind"],
            "provision_compact_openharmony_sdk_bundle",
        )
        self.assertEqual(
            raised.exception.details["retry_policy"],
            "stop_source_rewrites_until_environment_ready",
        )
        self.assertIn("install --source-root", raised.exception.details["provision_command_template"])

    def test_accelerator_standard_headers_compile_with_fixed_clang(self) -> None:
        try:
            experiment._toolchain(WORKSPACE_ROOT)
        except PlatformError as exc:
            self.skipTest(exc.message)
        run_root = self.state_root / "real-standard-header-compile"
        run_root.mkdir()
        source = run_root / "source.c"
        source.write_text(
            "#include <unistd.h>\n"
            "#include <stdio.h>\n"
            "#include <string.h>\n"
            "#include <stdlib.h>\n"
            "int main(void) { char *p = (char *)malloc(8); "
            "if (p) { memset(p, 0, 8); write(1, p, 1); free(p); } return 0; }\n",
            encoding="utf-8",
        )

        _, detail = experiment._compile_and_link(
            WORKSPACE_ROOT,
            self.state_root,
            run_root,
            source,
            "gpu_gles3",
            experiment.EXPERIMENT_PROFILES["gpu_gles3"],
            [],
        )

        self.assertEqual(detail["compile"]["return_code"], 0, detail["compile"]["stderr"])
        self.assertEqual(detail["runtime_harness"]["id"], "build_worker_startup_shim/v1")
        self.assertEqual(detail["runtime_harness"]["compile"]["return_code"], 0)

    def test_staging_rejects_macro_include_even_when_it_names_a_real_header(self) -> None:
        with self.assertRaises(PlatformError) as raised:
            self._stage('#define GPU_HEADER <GLES3/gl3.h>\n#include GPU_HEADER\nvoid _start(void) {}\n', profile_id="gpu_gles3")

        self.assertEqual(raised.exception.code, "DEVICE_EXPERIMENT_HEADER_DENIED")

    def test_freestanding_profile_reports_all_libc_headers_together(self) -> None:
        with self.assertRaises(PlatformError) as raised:
            self._stage(
                "#include <unistd.h>\n"
                "#include <stdio.h>\n"
                "#include <string.h>\n"
                "#include <stdlib.h>\n"
                "void _start(void) {}\n",
                profile_id="native_c",
            )

        self.assertEqual(raised.exception.code, "DEVICE_EXPERIMENT_HEADER_UNAVAILABLE")
        self.assertEqual(
            raised.exception.details["missing_headers"],
            ["unistd.h", "stdio.h", "string.h", "stdlib.h"],
        )

    def test_missing_header_returns_profile_query_guidance_instead_of_a_hidden_allowlist(self) -> None:
        with self.assertRaises(PlatformError) as raised:
            self._stage(
                "#include <GLES3/not_real.h>\n#include <EGL/also_missing.h>\nvoid _start(void) {}\n",
                profile_id="gpu_gles3",
            )

        self.assertEqual(raised.exception.code, "DEVICE_EXPERIMENT_HEADER_UNAVAILABLE")
        self.assertIn("device.describe_experiment_profiles", raised.exception.details["next_action_zh"])
        self.assertEqual(
            raised.exception.details["missing_headers"],
            ["GLES3/not_real.h", "EGL/also_missing.h"],
        )
        self.assertIn("GLES3/not_real.h", raised.exception.details["suggestions_by_header"])
        self.assertIn("EGL/also_missing.h", raised.exception.details["suggestions_by_header"])
        self.assertIn("GLES3/gl3.h", raised.exception.details["suggested_headers"])
        self.assertNotIn("available_headers", raised.exception.details)

    def test_profile_catalog_exposes_real_gpu_headers_and_supports_query(self) -> None:
        default_profile = experiment.experiment_profile_catalog(
            WORKSPACE_ROOT,
            state_root=self.state_root,
            profile_id="gpu_gles3",
        )[0]
        queried_profile = experiment.experiment_profile_catalog(
            WORKSPACE_ROOT,
            state_root=self.state_root,
            profile_id="gpu_gles3",
            header_query="gl32",
        )[0]
        standard_query = experiment.experiment_profile_catalog(
            WORKSPACE_ROOT,
            state_root=self.state_root,
            profile_id="gpu_gles3",
            header_query="stdio.h, string.h unistd.h",
        )[0]

        default_headers = default_profile["header_policy"]["matched_headers"]
        self.assertIn("EGL/egl.h", default_headers)
        self.assertIn("GLES3/gl3.h", default_headers)
        self.assertIn("stdio.h", default_headers)
        self.assertIn("stdlib.h", default_headers)
        self.assertIn("string.h", default_headers)
        self.assertIn("unistd.h", default_headers)
        self.assertEqual(queried_profile["header_policy"]["matched_headers"], ["GLES3/gl32.h"])
        self.assertEqual(
            standard_query["header_policy"]["matched_headers"],
            ["fortify/stdio.h", "fortify/string.h", "fortify/unistd.h", "stdio.h", "string.h", "unistd.h"],
        )
        self.assertEqual(
            standard_query["header_policy"]["header_query_terms"],
            ["stdio.h", "string.h", "unistd.h"],
        )
        self.assertEqual(default_profile["header_policy"]["mode"], "fixed_root_resolution")
        self.assertEqual(default_profile["libc_header_surface"]["family"], "openharmony_musl_aarch64")
        self.assertIn("stdio.h", default_profile["libc_header_surface"]["standard_c_headers"])
        self.assertTrue(default_profile["libc_header_surface"]["linked_device_libc"])
        self.assertIn("不要删除", default_profile["libc_header_surface"]["guidance_zh"])
        self.assertTrue(default_profile["libc_header_surface"]["generated_header_provenance"]["output_sha256"])
        self.assertEqual(default_profile["entrypoint"], "int main(void)")
        self.assertEqual(default_profile["binary_entrypoint"], "_start")
        self.assertEqual(default_profile["runtime_harness"], "build_worker_startup_shim/v1")
        self.assertIn("openharmony-sdk/interface/sdk_c/third_party/openGLES/GLES3/gl3.h", default_profile["repository_header_paths"])
        self.assertEqual(
            queried_profile["repository_header_paths"],
            ["openharmony-sdk/interface/sdk_c/third_party/openGLES/GLES3/gl32.h"],
        )

    def test_compile_uses_nostdinc_and_only_fixed_header_roots(self) -> None:
        run_root = self.state_root / "compile-policy"
        run_root.mkdir()
        source = run_root / "source.c"
        source.write_text("#include <stdint.h>\nvoid _start(void) {}\n", encoding="utf-8")
        commands: list[list[str]] = []

        def run_process(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(b"compiled")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(experiment, "_toolchain", return_value=(Path("/fixed/clang"), Path("/fixed/clang++"), Path("/fixed/ld.lld"))),
            patch.object(experiment, "_run_process", side_effect=run_process),
        ):
            binary, _ = experiment._compile_and_link(
                WORKSPACE_ROOT,
                self.state_root,
                run_root,
                source,
                "native_c",
                experiment.EXPERIMENT_PROFILES["native_c"],
                [],
            )

        self.assertIsNotNone(binary)
        compile_command = commands[0]
        link_command = commands[-1]
        self.assertIn("-nostdinc", compile_command)
        self.assertIn("--threads=1", link_command)
        include_roots = [compile_command[index + 1] for index, value in enumerate(compile_command) if value == "-isystem"]
        self.assertTrue(include_roots)
        self.assertTrue(all(
            Path(root).resolve().is_relative_to(self.state_root.resolve())
            for root in include_roots
        ))

    def test_gpu_profile_common_libc_headers_compile_with_fixed_roots(self) -> None:
        try:
            clang, _, _ = experiment._toolchain(WORKSPACE_ROOT)
        except PlatformError as exc:
            self.skipTest(exc.message)
        source = self.state_root / "libc-header-probe.c"
        source.write_text(
            "#include <assert.h>\n"
            "#include <errno.h>\n"
            "#include <math.h>\n"
            "#include <stdio.h>\n"
            "#include <stdlib.h>\n"
            "#include <string.h>\n"
            "#include <time.h>\n"
            "#include <unistd.h>\n"
            "#include <sys/syscall.h>\n"
            "int header_probe(void) { return STDOUT_FILENO + SYS_write + (errno == 0); }\n",
            encoding="utf-8",
        )
        profile = experiment.EXPERIMENT_PROFILES["gpu_gles3"]
        command = [
            str(clang),
            f"--target={profile['target']}",
            f"-std={profile['standard']}",
            "-nostdinc",
        ]
        for root in experiment._profile_header_roots(WORKSPACE_ROOT, self.state_root, profile):
            command.extend(["-isystem", str(root)])
        completed = subprocess.run(
            [*command, "-fsyntax-only", str(source)],
            cwd=self.state_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_changed_staged_source_is_rejected_before_build_or_hdc(self) -> None:
        staged = self._stage()
        path = self.state_root / str(staged["source_relative_path"])
        path.write_text("void _start(void) {}\n", encoding="utf-8")

        with self.assertRaises(PlatformError) as raised:
            experiment.build_and_upload_device_experiment(
                WORKSPACE_ROOT,
                self.state_root,
                probe_artifact=self.probe,
                staged_source_artifact=staged,
            )

        self.assertEqual(raised.exception.code, "DEVICE_EXPERIMENT_SOURCE_CHANGED")

    def test_fixed_profile_build_upload_returns_remote_path_without_executing_it(self) -> None:
        staged = self._stage()

        def compile_stub(_workspace: Path, run_root: Path, *_args: object) -> tuple[Path, dict[str, object]]:
            binary = run_root / "experiment.bin"
            binary.write_bytes(b"test-binary")
            return binary, {"compile": {"return_code": 0}, "link": {"return_code": 0}}

        completed = subprocess.CompletedProcess(args=["fake-hdc"], returncode=0, stdout="", stderr="")
        with patch.object(experiment, "resolve_hdc_binary", return_value=Path("/fixed/hdc")), \
                patch.object(experiment, "_available_devices", return_value=["test-device-01"]), \
                patch.object(experiment, "_pull_profile_libraries", return_value=(True, [], [])), \
                patch.object(experiment, "_compile_and_link", side_effect=compile_stub), \
                patch.object(experiment, "_run_hdc", return_value=completed) as hdc:
            result = experiment.build_and_upload_device_experiment(
                WORKSPACE_ROOT,
                self.state_root,
                probe_artifact=self.probe,
                staged_source_artifact=staged,
            )

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["deployment"]["remote_binary_path"].split("/")[-1], "experiment.bin")
        self.assertFalse(result["workstation_execution_boundary"]["host_shell"])
        self.assertIn("device.execute_shell", result["next_action_zh"])
        self.assertEqual(hdc.call_count, 2)  # remote mkdir + upload; execution belongs to a later action.


if __name__ == "__main__":
    unittest.main()
