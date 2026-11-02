from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPOSITORY_ROOT / "deploy" / "linux" / "heteroctl"
COMPATIBILITY_SCRIPT = REPOSITORY_ROOT / "deploy" / "linux" / "hetero-user.sh"
ENVIRONMENT_EXAMPLE = REPOSITORY_ROOT / "deploy" / "linux" / "platform.env.example"
WEB_GATEWAY = REPOSITORY_ROOT / "tools" / "serve-platform.mjs"
ROOTLESS_ONLY = unittest.skipIf(
    os.geteuid() == 0,
    "heteroctl intentionally rejects root",
)


class UserSpaceDeploymentTests(unittest.TestCase):
    def run_controller(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(DEPLOY_SCRIPT), *arguments],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def create_fake_executable(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    @staticmethod
    def create_fake_llvm_toolchain(root: Path) -> Path:
        bin_root = root / "bin"
        bin_root.mkdir(parents=True)
        for name in ("clang", "clang++", "ld.lld"):
            executable = bin_root / name
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
        return bin_root

    def test_scripts_are_valid_and_executable(self) -> None:
        for shell, script in (
            ("bash", DEPLOY_SCRIPT),
            ("sh", COMPATIBILITY_SCRIPT),
        ):
            result = subprocess.run(
                [shell, "-n", str(script)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.access(script, os.X_OK), script)

    def test_controller_contains_no_system_install_commands(self) -> None:
        executable_lines = [
            line.strip()
            for line in DEPLOY_SCRIPT.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        forbidden_prefixes = (
            "sudo ",
            "systemctl ",
            "apt ",
            "apt-get ",
            "dnf ",
            "yum ",
        )
        self.assertFalse(
            [
                line
                for line in executable_lines
                if line.startswith(forbidden_prefixes)
            ]
        )

    def test_offline_rebuild_does_not_require_git(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("require_command git", content)

    def test_web_readiness_checks_the_configured_listener_scope(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("web_http_ready && web_listener_matches_access_mode", content)
        self.assertIn("prepare_web_listener", content)
        self.assertIn("restarting owned ${scope} listener", content)
        self.assertIn("refusing to reuse an older or unrelated Web process", content)
        self.assertIn("prepare_api_listener", content)
        self.assertIn("refusing to reuse an older or unrelated API process", content)

    def test_loopback_api_readiness_bypasses_environment_proxy(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        function_start = content.index("api_ready() {")
        function_end = content.index("\nweb_http_ready() {", function_start)
        api_ready_function = content[function_start:function_end]
        self.assertIn("--noproxy '*'", api_ready_function)
        self.assertIn("/v1/live", api_ready_function)

        with tempfile.TemporaryDirectory(prefix=".api-ready-", dir=REPOSITORY_ROOT) as temp:
            document_root = Path(temp)
            (document_root / "v1").mkdir()
            (document_root / "v1" / "live").write_text('{"status":"ok"}\n', encoding="utf-8")
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            server = subprocess.Popen(
                [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(document_root)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(50):
                    with socket.socket() as probe:
                        if probe.connect_ex(("127.0.0.1", port)) == 0:
                            break
                    time.sleep(0.02)
                environment = {
                    **os.environ,
                    "HETERO_API_PORT": str(port),
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "http_proxy": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                    "no_proxy": "",
                }
                result = subprocess.run(
                    ["bash", "-c", f"{api_ready_function}\napi_ready"],
                    cwd=REPOSITORY_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
            finally:
                server.terminate()
                server.wait(timeout=5)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_browser_bootstrap_and_safe_install_are_part_of_deployment_contract(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("web_api_proxy_ready", content)
        self.assertIn("web_bootstrap_ready", content)
        self.assertIn("Install refused while this runtime is active", content)
        self.assertIn("active_app_runtime_processes", content)
        self.assertIn("repository is still used by active platform/build processes", content)
        self.assertIn("doctor) doctor_runtime", content)
        self.assertIn("python-check) python_check_runtime", content)
        self.assertIn("agent-check) agent_check_runtime", content)
        self.assertIn("web_static_assets_diagnostic", content)
        self.assertIn("web_static_assets_ready", content)
        self.assertIn("backend.tools.validate_next_build", content)
        self.assertIn(".next-production-backup", content)
        self.assertIn("--retain-from", content)
        self.assertIn("stop_reserved_port_listeners", content)
        self.assertIn("listener_pids_for_port", content)
        self.assertIn("--owned-only", content)
        self.assertIn("tools/serve-platform.mjs", content)

        gateway = WEB_GATEWAY.read_text(encoding="utf-8")
        self.assertIn("nextUpgradeRegistrationSink = new EventEmitter()", gateway)
        self.assertIn("httpServer: nextUpgradeRegistrationSink", gateway)
        self.assertIn("const nextUpgradeHandler = app.getUpgradeHandler()", gateway)
        self.assertIn("server.on('upgrade'", gateway)
        self.assertIn("/v1/device-bridges/poll", gateway)
        self.assertIn("/v1/device-bridges/ws", gateway)
        self.assertIn("HETERO_NEXT_API_PROXY_TARGET", gateway)

    def test_startup_readiness_has_heartbeat_deadline_and_process_exit_diagnostics(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        functions_start = content.index("startup_positive_integer() {")
        functions_end = content.index("\ninitialize_runtime() {", functions_start)
        readiness_functions = content[functions_start:functions_end]
        self.assertIn("HETERO_STARTUP_TIMEOUT_SECONDS:-120", readiness_functions)
        self.assertIn("HETERO_STARTUP_HEARTBEAT_SECONDS:-5", readiness_functions)
        self.assertIn("process exited before readiness", readiness_functions)
        self.assertIn("recent startup log", readiness_functions)
        self.assertIn("wait_ready api api_ready api", content)
        self.assertIn("wait_ready web web_ready web", content)
        self.assertIn("wait_ready web-api-proxy web_api_proxy_ready web api", content)
        self.assertIn("/v1/agent-cli/adapters", content)
        self.assertIn("--timeout-keep-alive", content)
        self.assertIn("HETERO_API_KEEP_ALIVE_SECONDS:-75", content)
        self.assertIn("--timeout-graceful-shutdown", content)
        self.assertIn("HETERO_API_GRACEFUL_SHUTDOWN_SECONDS:-6", content)

        with tempfile.TemporaryDirectory(prefix=".startup-readiness-", dir=REPOSITORY_ROOT) as temp:
            log_root = Path(temp)
            (log_root / "api.log").write_text("API startup failed: missing runtime asset\n", encoding="utf-8")
            script = f"""
set -u
{readiness_functions}
LOG_DIR="$1"
HETERO_STARTUP_TIMEOUT_SECONDS=10
HETERO_STARTUP_HEARTBEAT_SECONDS=1
HETERO_STARTUP_POLL_SECONDS=1
api_ready() {{ return 1; }}
pid_alive() {{ return 1; }}
if wait_ready api api_ready api; then
    exit 99
fi
probe_ready() {{ return 1; }}
HETERO_STARTUP_TIMEOUT_SECONDS=2
if wait_ready probe probe_ready; then
    exit 98
fi
"""
            result = subprocess.run(
                ["bash", "-c", script, "startup-readiness-test", str(log_root)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("api: waiting for readiness", result.stdout)
        self.assertIn("api process exited before readiness", result.stderr)
        self.assertIn("API startup failed: missing runtime asset", result.stderr)
        self.assertIn("probe: still starting (1s/2s)", result.stdout)
        self.assertIn("probe: readiness timeout after 2s", result.stderr)

    @ROOTLESS_ONLY
    def test_install_rejects_an_active_process_from_another_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            test_root = Path(temp)
            runtime_root = test_root / "runtime"
            proc_root = test_root / "proc"
            process_root = proc_root / "4242"
            process_root.mkdir(parents=True)
            (process_root / "cwd").symlink_to(REPOSITORY_ROOT, target_is_directory=True)
            (process_root / "cmdline").write_bytes(
                f"node\0{REPOSITORY_ROOT}/node_modules/.bin/next\0start\0".encode()
            )
            environment = {**os.environ, "HETERO_PROC_ROOT": str(proc_root)}

            result = self.run_controller(
                "install",
                "--skip-deps",
                "--skip-build",
                "--runtime-root",
                str(runtime_root),
                environment=environment,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("repository is still used by active platform/build processes", result.stderr)
            self.assertIn("4242", result.stderr)

    def test_install_reports_stage_progress_and_bounds_dependency_network_waits(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('run_install_stage "1/5" "Python 环境与依赖"', content)
        self.assertIn('run_install_stage "1a/5" "OpenHarmony 紧凑 SDK 与 musl 头文件"', content)
        self.assertIn('run_install_stage "2/5" "Kimi Code Agent Engine"', content)
        self.assertIn('run_install_stage "2a/5" "Agent Adapter 配置预检"', content)
        self.assertIn('run_install_stage "3/5" "Web 前端依赖"', content)
        self.assertIn('run_install_stage "4/5" "Next.js 生产构建"', content)
        self.assertIn('run_install_stage "5/5" "部署预检"', content)
        self.assertIn("HETERO_INSTALL_SLOW_OPERATION_SECONDS:-60", content)
        self.assertIn("HETERO_INSTALL_HEARTBEAT_SECONDS", content)
        self.assertIn('run_install_operation "安装 Python 依赖（pip install）"', content)
        self.assertIn('run_install_operation "安装 Web 前端依赖（npm ci）"', content)
        self.assertIn('run_install_operation "执行 Next.js 生产构建（npm run build）"', content)
        self.assertIn("当前操作：${operation_label}", content)
        self.assertIn("失败操作：${failed_operation_label}", content)
        self.assertIn("CWD: ${working_directory}", content)
        self.assertIn("npm_config_fetch_timeout=120000", content)
        self.assertIn('HETERO_NPM_REGISTRY:-https://registry.npmjs.org/', content)
        self.assertIn('--replace-registry-host="$replace_registry_host"', content)
        self.assertIn('replace_registry_host="never"', content)
        self.assertIn("--skip-python-deps", content)
        self.assertIn("--python-bin", content)
        self.assertIn("HETERO_PYTHON_BIN", content)
        self.assertIn(".venv-python-backup", content)
        self.assertIn("--skip-web-deps", content)
        self.assertIn("--skip-kimi", content)
        self.assertIn("--foreground-scripts --loglevel=http --timing", content)
        self.assertIn("npm 调试日志", content)
        self.assertIn("install or sdk to build the compact SDK", content)
        self.assertIn("backend.tools.provision_openharmony_sdk_bundle", content)

    def test_install_bootstraps_pip_when_an_existing_venv_has_no_pip(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        functions_start = content.index("ensure_virtualenv_pip() {")
        functions_end = content.index("\npatch_kimi_installer_curl_insecure() {", functions_start)
        install_functions = content[functions_start:functions_end]

        with tempfile.TemporaryDirectory(prefix=".pip-bootstrap-", dir=REPOSITORY_ROOT) as temp:
            app_root = Path(temp)
            python_bin = app_root / ".venv" / "bin" / "python"
            python_bin.parent.mkdir(parents=True)
            (app_root / "backend").mkdir()
            (app_root / "backend" / "requirements.txt").write_text("", encoding="utf-8")
            python_bin.write_text(
                """#!/usr/bin/env bash
set -eu
case "$*" in
  "-m pip --version") test -f "$FAKE_PIP_STATE" ;;
  "-m ensurepip --upgrade") touch "$FAKE_PIP_STATE" ;;
  "-m pip install "*)
    test -f "$FAKE_PIP_STATE"
    touch "$FAKE_INSTALL_STATE"
    ;;
  *) exit 2 ;;
esac
""",
                encoding="utf-8",
            )
            python_bin.chmod(0o755)
            pip_state = app_root / "pip-ready"
            install_state = app_root / "requirements-installed"
            cache_dir = app_root / "cache"
            cache_dir.mkdir()
            script = f"""
set -euo pipefail
APP_ROOT="$1"
CACHE_DIR="$2"
PYTHON_BIN="${{APP_ROOT}}/.venv/bin/python"
REQUESTED_PYTHON_BIN=""
resolve_python_bin() {{ :; }}
python_version_supported() {{ return 0; }}
python_version_string() {{ printf '%s\\n' '3.11.0'; }}
python_base_executable_identity() {{ printf '%s\\n' '/usr/bin/python3.11'; }}
python_executable_identity() {{ printf '%s\\n' '/usr/bin/python3.11'; }}
run_install_operation() {{
    shift
    "$@"
}}
{install_functions}
install_python_dependencies
"""
            result = subprocess.run(
                ["bash", "-c", script, "pip-bootstrap-test", str(app_root), str(cache_dir)],
                cwd=REPOSITORY_ROOT,
                env={
                    **os.environ,
                    "FAKE_PIP_STATE": str(pip_state),
                    "FAKE_INSTALL_STATE": str(install_state),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(pip_state.is_file())
            self.assertTrue(install_state.is_file())
            self.assertIn("ensurepip", result.stdout)

    def test_install_rebuilds_a_python_310_virtualenv_with_the_selected_interpreter(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        functions_start = content.index("ensure_virtualenv_pip() {")
        functions_end = content.index("\npatch_kimi_installer_curl_insecure() {", functions_start)
        install_functions = content[functions_start:functions_end]

        with tempfile.TemporaryDirectory(prefix=".python-migrate-", dir=REPOSITORY_ROOT) as temp:
            app_root = Path(temp)
            cache_dir = app_root / "cache"
            cache_dir.mkdir()
            (app_root / "backend").mkdir()
            (app_root / "backend" / "requirements.txt").write_text("", encoding="utf-8")
            selected_python = app_root / "python3.11"
            selected_python.write_text(
                f"""#!/usr/bin/env bash
set -eu
if [[ "${{1:-}}" == "-c" ]]; then
  case "${{2:-}}" in
    *platform.python_version*) printf '%s\\n' '3.11.9'; exit 0 ;;
    *sys.version_info*) exit 0 ;;
    *'_base_executable'*) printf '%s\\n' '{selected_python}'; exit 0 ;;
    *sys.executable*) printf '%s\\n' '{selected_python}'; exit 0 ;;
  esac
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
  chmod +x "$3/bin/python"
  exit 0
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "pip" && "${{3:-}}" == "--version" ]]; then
  exit 0
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "pip" && "${{3:-}}" == "install" ]]; then
  touch "$FAKE_INSTALL_STATE"
  exit 0
fi
exit 2
""",
                encoding="utf-8",
            )
            selected_python.chmod(0o755)
            old_python = app_root / ".venv" / "bin" / "python"
            old_python.parent.mkdir(parents=True)
            old_python.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == "-c" ]]; then
  case "${2:-}" in
    *platform.python_version*) printf '%s\n' '3.10.14'; exit 0 ;;
    *sys.version_info*) exit 1 ;;
  esac
fi
exit 2
""",
                encoding="utf-8",
            )
            old_python.chmod(0o755)
            install_state = app_root / "requirements-installed"
            script = f"""
set -euo pipefail
APP_ROOT="$1"
CACHE_DIR="$2"
PYTHON_BIN="$3"
PYTHON_VERSION="3.11.9"
REQUESTED_PYTHON_BIN="$3"
resolve_python_bin() {{ :; }}
python_version_supported() {{ "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; }}
python_version_string() {{ "$1" -c 'import platform; print(platform.python_version())'; }}
python_base_executable_identity() {{ "$1" -c 'import os,sys; print(os.path.realpath(getattr(sys, "_base_executable", sys.executable)))'; }}
python_executable_identity() {{ "$1" -c 'import os,sys; print(os.path.realpath(sys.executable))'; }}
run_install_operation() {{
    shift
    "$@"
}}
{install_functions}
install_python_dependencies
"""
            result = subprocess.run(
                ["bash", "-c", script, "python-migrate-test", str(app_root), str(cache_dir), str(selected_python)],
                cwd=REPOSITORY_ROOT,
                env={**os.environ, "FAKE_INSTALL_STATE": str(install_state)},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Python 3.10.14", result.stdout)
            self.assertIn("将使用", result.stdout)
            self.assertTrue(install_state.is_file())
            migrated = subprocess.check_output(
                [str(app_root / ".venv" / "bin" / "python"), "-c", "import platform; print(platform.python_version())"],
                text=True,
            ).strip()
            self.assertEqual(migrated, "3.11.9")
            self.assertEqual(list(app_root.glob(".venv-python-backup.*")), [])

    def test_slow_install_operation_reports_its_specific_name(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        functions_start = content.index("render_cli_command() {")
        functions_end = content.index("\nskip_install_stage() {", functions_start)
        monitor_functions = content[functions_start:functions_end]
        with tempfile.TemporaryDirectory(prefix=".install-monitor-", dir=REPOSITORY_ROOT) as temp:
            script = f"""
set -euo pipefail
{monitor_functions}
TMP_DIR="$1"
APP_ROOT="$PWD"
HETERO_INSTALL_SLOW_OPERATION_SECONDS=1
HETERO_INSTALL_HEARTBEAT_SECONDS=1
HETERO_INSTALL_MONITOR_POLL_SECONDS=1
run_install_stage "T/1" "安装监控测试" run_install_operation "测试慢操作" sleep 3
"""
            result = subprocess.run(
                ["bash", "-c", script, "install-monitor-test", temp],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("当前操作：测试慢操作", result.stdout)
        self.assertIn("CLI: sleep 3", result.stdout)
        self.assertGreaterEqual(result.stdout.count("CLI: sleep 3"), 2)
        self.assertIn("安装监控测试完成", result.stdout)

    def test_failed_install_stage_reports_the_exact_operation_and_cli(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        functions_start = content.index("render_cli_command() {")
        functions_end = content.index("\nskip_install_stage() {", functions_start)
        monitor_functions = content[functions_start:functions_end]
        with tempfile.TemporaryDirectory(prefix=".install-failure-", dir=REPOSITORY_ROOT) as temp:
            script = f"""
set -euo pipefail
{monitor_functions}
TMP_DIR="$1"
APP_ROOT="$PWD"
run_install_stage "T/1" "失败诊断测试" run_install_operation "具体失败操作" sh -c 'exit 7'
"""
            result = subprocess.run(
                ["bash", "-c", script, "install-failure-test", temp],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

        self.assertEqual(result.returncode, 7)
        self.assertIn("失败操作：具体失败操作", result.stderr)
        self.assertIn("CLI: sh -c exit\\ 7", result.stderr)

    def test_kimi_install_uses_runtime_scoped_official_installer_and_checksum(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('HETERO_KIMI_VERSION:-latest', content)
        self.assertIn('https://code.kimi.com/kimi-code/latest', content)
        self.assertIn('https://code.kimi.com/kimi-code/install.sh', content)
        self.assertIn("curl -k --proto '=https'", content)
        self.assertGreaterEqual(content.count("curl -k -fsSL --proto '=https'"), 2)
        self.assertIn('patch_kimi_installer_curl_insecure "$installer_path"', content)
        self.assertIn('curl_token.subn("curl -k", line)', content)
        self.assertIn('KIMI_INSTALL_DIR="$install_root"', content)
        self.assertIn("KIMI_NO_MODIFY_PATH=1", content)
        self.assertIn('https://code.kimi.com/kimi-code/binaries/${version}/manifest.json', content)
        self.assertIn("hashlib.sha256(path.read_bytes()).hexdigest()", content)
        self.assertIn("Kimi binary checksum mismatch", content)
        self.assertNotIn('HETERO_KIMI_VERSION:-0.31.1', content)
        self.assertNotIn('binaries/${version}/${filename}', content)
        self.assertIn("wait_ready kimi kimi_ready api", content)
        self.assertIn("backend-managed Agent Engine service is ready", content)
        self.assertIn("stale_web_build_source", content)
        self.assertIn("Production Web build is stale", content)
        self.assertIn("install --skip-deps --runtime-root", content)

    def test_kimi_installer_rewriter_adds_insecure_flag_to_curl_commands(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        functions_start = content.index("patch_kimi_installer_curl_insecure() {")
        functions_end = content.index("\n}\n\ninstall_kimi_code() {", functions_start) + 3
        patch_function = content[functions_start:functions_end]

        with tempfile.TemporaryDirectory(prefix=".kimi-installer-patch-", dir=REPOSITORY_ROOT) as temp:
            installer = Path(temp) / "install.sh"
            installer.write_text(
                "#!/usr/bin/env bash\n"
                "curl -fsSL https://example.invalid/binary\n"
                "payload=\"$(curl --location https://example.invalid/version)\"\n"
                "curl -k -fsSL https://example.invalid/already-patched\n"
                "curl --insecure https://example.invalid/long-option\n"
                "# curl https://example.invalid/comment\n",
                encoding="utf-8",
            )
            script = f"set -euo pipefail\nPYTHON_BIN=\"$2\"\n{patch_function}\npatch_kimi_installer_curl_insecure \"$1\"\n"
            result = subprocess.run(
                ["bash", "-c", script, "kimi-installer-patch-test", str(installer), sys.executable],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            patched = installer.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Patched 2 curl command(s)", result.stdout)
        self.assertIn("curl -k -fsSL https://example.invalid/binary", patched)
        self.assertIn("$(curl -k --location https://example.invalid/version)", patched)
        self.assertEqual(patched.count("curl -k -fsSL"), 2)
        self.assertIn("curl --insecure https://example.invalid/long-option", patched)
        self.assertIn("# curl https://example.invalid/comment", patched)

    @ROOTLESS_ONLY
    def test_status_reports_the_observed_listener_scope(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            test_root = Path(temp)
            runtime_root = test_root / "runtime"
            proc_net_root = test_root / "proc-net"
            proc_net_root.mkdir()
            initialized = self.run_controller("init", "--remote", "--runtime-root", str(runtime_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            with (runtime_root / "platform.env").open("a", encoding="utf-8") as stream:
                stream.write(f'HETERO_PROC_NET_ROOT="{proc_net_root}"\n')
            (proc_net_root / "tcp").write_text(
                "  sl  local_address rem_address   st tx_queue rx_queue\n"
                "   0: 0100007F:0FC0 00000000:0000 0A 00000000:00000000 00:00000000\n",
                encoding="utf-8",
            )

            result = self.run_controller("status", "--runtime-root", str(runtime_root))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("web-listener: local (expected remote)", result.stdout)

    @ROOTLESS_ONLY
    @unittest.skipUnless(
        (REPOSITORY_ROOT / ".venv" / "bin" / "python").is_file()
        and (REPOSITORY_ROOT / "node_modules" / ".bin" / "next").is_file()
        and (REPOSITORY_ROOT / ".next" / "BUILD_ID").is_file(),
        "listener ownership smoke test requires an installed production checkout",
    )
    def test_start_rejects_a_healthy_unowned_web_listener(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            test_root = Path(temp)
            runtime_root = test_root / "runtime"
            proc_net_root = test_root / "proc-net"
            proc_net_root.mkdir()
            initialized = self.run_controller("init", "--remote", "--runtime-root", str(runtime_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            with (runtime_root / "platform.env").open("a", encoding="utf-8") as stream:
                stream.write(f'HETERO_PROC_NET_ROOT="{proc_net_root}"\n')
            (proc_net_root / "tcp").write_text(
                "  sl  local_address rem_address   st tx_queue rx_queue\n"
                "   0: 00000000:0FC0 00000000:0000 0A 00000000:00000000 00:00000000\n",
                encoding="utf-8",
            )

            result = self.run_controller("start", "--runtime-root", str(runtime_root))

            self.assertEqual(result.returncode, 1)
            self.assertIn("refusing to reuse an older or unrelated Web process", result.stderr)

    @ROOTLESS_ONLY
    def test_stop_releases_an_unowned_current_user_listener(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            test_root = Path(temp)
            runtime_root = test_root / "runtime"
            proc_root = test_root / "proc"
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            initialized = self.run_controller("init", "--runtime-root", str(runtime_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            environment_file = runtime_root / "platform.env"
            content = environment_file.read_text(encoding="utf-8")
            content = content.replace("HETERO_WEB_PORT=4032", f"HETERO_WEB_PORT={port}")
            content = content.replace("HETERO_API_PORT=8006", "HETERO_API_PORT=65534")
            environment_file.write_text(content, encoding="utf-8")
            server = subprocess.Popen(
                [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(50):
                    with socket.socket() as probe:
                        if probe.connect_ex(("127.0.0.1", port)) == 0:
                            break
                    time.sleep(0.05)

                process_root = proc_root / str(server.pid)
                descriptor_root = process_root / "fd"
                descriptor_root.mkdir(parents=True)
                (descriptor_root / "3").symlink_to("socket:[12345]")
                net_root = proc_root / "net"
                net_root.mkdir()
                (net_root / "tcp").write_text(
                    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
                    f"   0: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 "
                    f"00000000 {os.getuid()} 0 12345 1\n",
                    encoding="ascii",
                )
                def clear_fake_listener_after_exit() -> None:
                    server.wait()
                    (net_root / "tcp").write_text(
                        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n",
                        encoding="ascii",
                    )

                reaper = threading.Thread(target=clear_fake_listener_after_exit, daemon=True)
                reaper.start()
                environment = {
                    **os.environ,
                    "HETERO_PROC_ROOT": str(proc_root),
                    "HETERO_PROC_NET_ROOT": str(net_root),
                }

                result = self.run_controller(
                    "stop",
                    "--runtime-root",
                    str(runtime_root),
                    environment=environment,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                reaper.join(timeout=5)
                with socket.socket() as probe:
                    self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
            finally:
                if server.poll() is None:
                    server.terminate()
                    server.wait(timeout=5)

    def test_reference_environment_uses_only_user_paths(self) -> None:
        content = ENVIRONMENT_EXAMPLE.read_text(encoding="utf-8")
        for forbidden in (
            "/var/lib/",
            "/opt/",
            "/etc/",
            "/usr/local/",
        ):
            self.assertNotIn(forbidden, content)

    @ROOTLESS_ONLY
    def test_init_writes_only_below_selected_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            test_root = Path(temp)
            user_home = test_root / "home"
            runtime_root = test_root / "runtime"
            user_home.mkdir()
            environment = {**os.environ, "HOME": str(user_home)}

            result = self.run_controller(
                "init",
                "--runtime-root",
                str(runtime_root),
                environment=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            expected_directories = {"assets", "cache", "logs", "run", "state", "tmp"}
            self.assertTrue(
                expected_directories.issubset(
                    {path.name for path in runtime_root.iterdir() if path.is_dir()}
                )
            )
            environment_file = runtime_root / "platform.env"
            self.assertTrue(environment_file.is_file())
            self.assertEqual(environment_file.stat().st_mode & 0o777, 0o600)
            content = environment_file.read_text(encoding="utf-8")
            self.assertIn(str(runtime_root / "state"), content)
            self.assertIn(str(runtime_root / "assets"), content)
            self.assertIn(str(runtime_root / "assets" / "kimi-code" / "bin" / "kimi"), content)
            self.assertIn(str(runtime_root / "state" / "kimi-code"), content)
            self.assertIn("HETERO_KIMI_AUTO_START=1", content)
            self.assertIn("KIMI_DISABLE_TELEMETRY=1", content)
            self.assertIn("NEXT_TELEMETRY_DISABLED=1", content)
            self.assertIn("HETERO_ACCESS_MODE=remote", content)
            self.assertIn("HETERO_WEB_HOST=0.0.0.0", content)
            self.assertIn("HETERO_WEB_PORT=4032", content)
            self.assertIn("HETERO_WORKBENCH_URL=http://127.0.0.1:4032", content)
            self.assertIn("HETERO_API_HOST=127.0.0.1", content)
            for forbidden in ("/var/lib/", "/opt/", "/etc/"):
                self.assertNotIn(forbidden, content)

    @ROOTLESS_ONLY
    def test_local_mode_restricts_the_web_listener(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            runtime_root = Path(temp) / "runtime"

            result = self.run_controller(
                "init",
                "--local",
                "--runtime-root",
                str(runtime_root),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            content = (runtime_root / "platform.env").read_text(encoding="utf-8")
            self.assertIn("HETERO_ACCESS_MODE=local", content)
            self.assertIn("HETERO_WEB_HOST=127.0.0.1", content)
            self.assertIn("HETERO_API_HOST=127.0.0.1", content)

    @ROOTLESS_ONLY
    def test_explicit_mode_updates_an_existing_environment_without_resetting_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            runtime_root = Path(temp) / "runtime"
            initialized = self.run_controller("init", "--local", "--runtime-root", str(runtime_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            environment_file = runtime_root / "platform.env"
            with environment_file.open("a", encoding="utf-8") as stream:
                stream.write("HETERO_TEST_PRESERVED=keep-me\n")

            updated = self.run_controller("init", "--remote", "--runtime-root", str(runtime_root))

            self.assertEqual(updated.returncode, 0, updated.stderr)
            content = environment_file.read_text(encoding="utf-8")
            self.assertIn("HETERO_ACCESS_MODE=remote", content)
            self.assertIn("HETERO_WEB_HOST=0.0.0.0", content)
            self.assertIn("HETERO_TEST_PRESERVED=keep-me", content)

    @ROOTLESS_ONLY
    def test_web_port_is_validated_and_persisted_without_resetting_environment(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            runtime_root = Path(temp) / "runtime"
            initialized = self.run_controller(
                "init",
                "--web-port",
                "44032",
                "--runtime-root",
                str(runtime_root),
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            environment_file = runtime_root / "platform.env"
            with environment_file.open("a", encoding="utf-8") as stream:
                stream.write("HETERO_TEST_PRESERVED=keep-me\n")

            updated = self.run_controller(
                "init",
                "--web-port=44033",
                "--runtime-root",
                str(runtime_root),
            )

            self.assertEqual(updated.returncode, 0, updated.stderr)
            content = environment_file.read_text(encoding="utf-8")
            self.assertIn("HETERO_WEB_PORT=44033", content)
            self.assertIn("HETERO_WORKBENCH_URL=http://127.0.0.1:44033", content)
            self.assertIn("HETERO_TEST_PRESERVED=keep-me", content)

    def test_web_port_rejects_invalid_values(self) -> None:
        for value in ("0", "65536", "abc", "12.5"):
            result = self.run_controller("paths", "--web-port", value)
            self.assertEqual(result.returncode, 2, (value, result.stderr))
            self.assertIn("integer from 1 to 65535", result.stderr)

    @ROOTLESS_ONLY
    def test_python_interpreter_can_be_selected_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".python-select-", dir=REPOSITORY_ROOT) as temp:
            runtime_root = Path(temp) / "runtime"
            selected = subprocess.check_output(
                [sys.executable, "-c", "import os,sys; print(os.path.realpath(getattr(sys, '_base_executable', sys.executable)))"],
                text=True,
            ).strip()

            result = self.run_controller(
                "init",
                "--python-bin",
                sys.executable,
                "--runtime-root",
                str(runtime_root),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            content = (runtime_root / "platform.env").read_text(encoding="utf-8")
            self.assertIn(f'HETERO_PYTHON_BIN="{selected}"', content)

    @ROOTLESS_ONLY
    def test_deployment_mode_defaults_persists_and_can_switch(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key not in {"HETERO_DEPLOYMENT_MODE", "HETERO_AGENT_ENGINE"}}
        with tempfile.TemporaryDirectory(prefix=".deployment-mode-", dir=REPOSITORY_ROOT) as temp:
            root = Path(temp) / "runtime"
            for flags, expected in (
                ([], "device"),
                (["--worker-only"], "worker"),
                ([], "worker"),
                (["--analysis-only"], "analysis"),
                ([], "analysis"),
                (["--with-device-validation"], "device"),
            ):
                with self.subTest(flags=flags, expected=expected):
                    result = self.run_controller("init", "--runtime-root", str(root), *flags, environment=environment)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    content = (root / "platform.env").read_text()
                    self.assertIn(f'HETERO_DEPLOYMENT_MODE="{expected}"', content)
                    engine = "legacy" if expected == "worker" else "kimi_server"
                    self.assertRegex(content, rf'HETERO_AGENT_ENGINE="?{engine}"?\n')

    @ROOTLESS_ONLY
    @unittest.skipUnless((REPOSITORY_ROOT / ".venv" / "bin" / "python").is_file(), "worker install reuse test requires the managed Python environment")
    def test_worker_install_reuses_python_without_kimi_or_web_dependencies(self) -> None:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("HETERO_")}
        environment["HETERO_PYTHON_BIN"] = str(REPOSITORY_ROOT / ".venv" / "bin" / "python")
        with tempfile.TemporaryDirectory(prefix=".worker-deploy-", dir=REPOSITORY_ROOT) as temp:
            runtime_root = Path(temp) / "runtime"
            result = self.run_controller(
                "install", "--worker-only", "--skip-python-deps", "--runtime-root", str(runtime_root),
                environment=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Rootless Worker installation ready", result.stdout)
            self.assertNotIn("Kimi Code Agent Engine", result.stdout)
            self.assertNotIn("Web 前端依赖", result.stdout)
            content = (runtime_root / "platform.env").read_text(encoding="utf-8")
            self.assertIn('HETERO_DEPLOYMENT_MODE="worker"', content)
            self.assertIn('HETERO_AGENT_ENGINE="legacy"', content)

    @ROOTLESS_ONLY
    @unittest.skipUnless((REPOSITORY_ROOT / ".venv" / "bin" / "python").is_file(), "worker preflight test requires the managed Python environment")
    def test_worker_preflight_ignores_server_agent_configuration(self) -> None:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("HETERO_")}
        environment["HETERO_PYTHON_BIN"] = str(REPOSITORY_ROOT / ".venv" / "bin" / "python")
        with tempfile.TemporaryDirectory(prefix=".worker-preflight-", dir=REPOSITORY_ROOT) as temp:
            runtime_root = Path(temp) / "runtime"
            initialized = self.run_controller(
                "init", "--worker-only", "--runtime-root", str(runtime_root), environment=environment,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            env_file = runtime_root / "platform.env"
            content = env_file.read_text(encoding="utf-8").replace(
                'HETERO_AGENT_CLI_CONFIG=""',
                'HETERO_AGENT_CLI_CONFIG="/definitely/missing/agent-adapters.json"',
            )
            env_file.write_text(content, encoding="utf-8")

            checked = self.run_controller(
                "preflight", "--runtime-root", str(runtime_root), environment=environment,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("Deployment mode: worker", checked.stdout)

    def test_worker_start_gates_core_trace_and_build_before_api_start(self) -> None:
        content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        start = content[content.index("start_runtime() {"):content.index("\nstop_runtime() {")]
        gate = start.index("run_deployment_preflight --require-trace --require-build")
        api_start = start.index("start_owned_process api")
        self.assertLess(gate, api_start)
        self.assertNotIn("check_deployment_resources --require-trace --require-build", start)

    @ROOTLESS_ONLY
    def test_analysis_mode_ignores_unavailable_device_resource_overrides(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deployment-mode-", dir=REPOSITORY_ROOT) as temp:
            root = Path(temp)
            environment = {
                **os.environ,
                "HETERO_DEPLOYMENT_MODE": "analysis",
                **{key: str(root / "unavailable") for key in (
                    "HETERO_TRACE_STREAMER", "HETERO_LLVM_BIN_ROOT", "HETERO_GLSLANG_BIN",
                    "HETERO_OPENHARMONY_SDK_ROOT", "HETERO_RUNTIME_LIBRARY_ROOT", "HETERO_HDC_BIN",
                )},
            }
            result = self.run_controller("init", "--runtime-root", str(root / "runtime"), environment=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('HETERO_DEPLOYMENT_MODE="analysis"', (root / "runtime" / "platform.env").read_text())

    @ROOTLESS_ONLY
    def test_persisted_python_interpreter_is_reused_without_exporting_it_again(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".python-persist-", dir=REPOSITORY_ROOT) as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            marker = root / "selected-python-used"
            selected_python = root / "python3.11"
            selected_python.write_text(
                """#!/bin/sh
case "${2:-}" in
  *'_base_executable'*|*'sys.executable'*) printf '%s\\n' "$FAKE_PYTHON_ID"; exit 0 ;;
esac
: > "$FAKE_PYTHON_MARKER"
exec "$FAKE_PYTHON_REAL" "$@"
""",
                encoding="utf-8",
            )
            selected_python.chmod(0o755)
            environment = {
                **os.environ,
                "FAKE_PYTHON_ID": str(selected_python),
                "FAKE_PYTHON_MARKER": str(marker),
                "FAKE_PYTHON_REAL": sys.executable,
            }
            environment.pop("HETERO_PYTHON_BIN", None)

            initialized = self.run_controller(
                "init",
                "--python-bin",
                str(selected_python),
                "--runtime-root",
                str(runtime_root),
                environment=environment,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            marker.unlink(missing_ok=True)

            reused = self.run_controller(
                "paths",
                "--runtime-root",
                str(runtime_root),
                environment=environment,
            )

            self.assertEqual(reused.returncode, 0, reused.stderr)
            self.assertTrue(marker.is_file(), reused.stdout)

    @ROOTLESS_ONLY
    def test_python_interpreter_rejects_python_310_before_install(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".python-reject-", dir=REPOSITORY_ROOT) as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            python310 = root / "python3.10"
            python310.write_text(
                """#!/bin/sh
case "$2" in
  *platform.python_version*) printf '%s\\n' '3.10.14'; exit 0 ;;
  *sys.version_info*) exit 1 ;;
esac
exit 1
""",
                encoding="utf-8",
            )
            python310.chmod(0o755)
            probe = subprocess.run(
                [str(python310), "-c", "import platform; print(platform.python_version())"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.strip(), "3.10.14")

            result = self.run_controller(
                "paths",
                "--python-bin",
                str(python310),
                "--runtime-root",
                str(runtime_root),
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Python 3.11+", result.stderr)
            self.assertIn("3.10.14", result.stderr)

    @ROOTLESS_ONLY
    def test_llvm_bin_root_is_validated_and_persisted_from_option_or_environment(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            first = self.create_fake_llvm_toolchain(root / "first llvm")
            second = self.create_fake_llvm_toolchain(root / "second llvm")

            initialized = self.run_controller(
                "init",
                "--llvm-bin-root",
                str(first),
                "--runtime-root",
                str(runtime_root),
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            environment_file = runtime_root / "platform.env"
            with environment_file.open("a", encoding="utf-8") as stream:
                stream.write("HETERO_TEST_PRESERVED=keep-me\n")
            self.assertIn(f'HETERO_LLVM_BIN_ROOT="{first.resolve()}"', environment_file.read_text(encoding="utf-8"))

            environment = {**os.environ, "HETERO_LLVM_BIN_ROOT": str(second)}
            updated = self.run_controller(
                "init",
                "--runtime-root",
                str(runtime_root),
                environment=environment,
            )

            self.assertEqual(updated.returncode, 0, updated.stderr)
            content = environment_file.read_text(encoding="utf-8")
            self.assertIn(f'HETERO_LLVM_BIN_ROOT="{second.resolve()}"', content)
            self.assertIn("HETERO_TEST_PRESERVED=keep-me", content)

    @ROOTLESS_ONLY
    def test_resource_environment_overrides_update_existing_runtime_without_refresh(self) -> None:
        # Deployment resource variables are deliberate controller inputs.  Keep
        # this persistence test independent from a developer's active runtime;
        # otherwise an exported resource (for example HETERO_TRACE_STREAMER)
        # correctly acts as a new override during the final "preserve" call.
        base_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("HETERO_")
        }
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            initialized = self.run_controller(
                "init",
                "--runtime-root",
                str(runtime_root),
                environment=base_environment,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)

            glslang = self.create_fake_executable(root / "tooling" / "glslang")
            trace_streamer = self.create_fake_executable(root / "tooling" / "trace_streamer")
            hdc = self.create_fake_executable(root / "tooling" / "hdc")
            opencode = self.create_fake_executable(root / "tooling" / "opencode")
            llvm_bin_root = self.create_fake_llvm_toolchain(root / "tooling" / "llvm")
            sdk_root = root / "sdk"
            library_root = root / "libraries"
            build_library_root = root / "build-libraries"
            source_root = root / "source"
            fixture_root = root / "fixtures"
            for directory in (sdk_root, library_root, build_library_root, source_root, fixture_root):
                directory.mkdir()
            adapter_config = root / "agent-cli-adapters.json"
            adapter_config.write_text("{}\n", encoding="utf-8")
            overrides = {
                "HETERO_GLSLANG_BIN": glslang,
                "HETERO_TRACE_STREAMER": trace_streamer,
                "HETERO_LLVM_BIN_ROOT": llvm_bin_root,
                "HETERO_OPENHARMONY_SDK_ROOT": sdk_root,
                "HETERO_RUNTIME_LIBRARY_ROOT": library_root,
                "HETERO_BUILD_RUNTIME_LIBRARY_ROOT": build_library_root,
                "HETERO_HDC_BIN": hdc,
                "HETERO_OPENCODE_BIN": opencode,
                "HETERO_AGENT_CLI_CONFIG": adapter_config,
                "HETERO_DEFAULT_SOURCE_ROOT": source_root,
                "HETERO_TRACE_FIXTURE_ROOT": fixture_root,
            }
            environment = {**base_environment, **{key: str(value) for key, value in overrides.items()}}
            updated = self.run_controller(
                "init",
                "--runtime-root",
                str(runtime_root),
                environment=environment,
            )

            self.assertEqual(updated.returncode, 0, updated.stderr)
            self.assertIn("Updated resource environment", updated.stdout)
            content = (runtime_root / "platform.env").read_text(encoding="utf-8")
            for key, value in overrides.items():
                self.assertIn(f'{key}="{value.resolve()}"', content)

            preserved = self.run_controller(
                "init",
                "--runtime-root",
                str(runtime_root),
                environment=base_environment,
            )
            self.assertEqual(preserved.returncode, 0, preserved.stderr)
            preserved_content = (runtime_root / "platform.env").read_text(encoding="utf-8")
            for key, value in overrides.items():
                self.assertIn(f'{key}="{value.resolve()}"', preserved_content)

    @ROOTLESS_ONLY
    def test_agent_check_validates_and_persists_a_custom_default_adapter(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".agent-check-", dir=REPOSITORY_ROOT) as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            binary = self.create_fake_executable(root / "codeagent")
            manifest = root / "agent-cli-adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-codeagent",
                "display_name": "Team CodeAgent",
                "executable": str(binary),
            }]}), encoding="utf-8")
            environment = {
                **os.environ,
                "HETERO_AGENT_CLI_CONFIG": str(manifest),
                "HETERO_AGENT_CLI_ADAPTER": "team-codeagent",
            }

            checked = self.run_controller(
                "agent-check",
                "--runtime-root",
                str(runtime_root),
                environment=environment,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("Default Agent Adapter: team-codeagent", checked.stdout)
            self.assertIn("[READY] team-codeagent", checked.stdout)

            initialized = self.run_controller(
                "init",
                "--runtime-root",
                str(runtime_root),
                environment=environment,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            platform_environment = (runtime_root / "platform.env").read_text(encoding="utf-8")
            self.assertIn(f'HETERO_AGENT_CLI_CONFIG="{manifest.resolve()}"', platform_environment)
            self.assertIn('HETERO_AGENT_CLI_ADAPTER="team-codeagent"', platform_environment)

    @ROOTLESS_ONLY
    def test_llvm_bin_root_rejects_missing_tools(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            incomplete = root / "llvm" / "bin"
            incomplete.mkdir(parents=True)
            for name in ("clang", "clang++"):
                executable = incomplete / name
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)

            result = self.run_controller(
                "init",
                "--llvm-bin-root",
                str(incomplete),
                "--runtime-root",
                str(runtime_root),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("must contain executable ld.lld", result.stderr)

    @ROOTLESS_ONLY
    def test_start_accepts_runtime_library_root_before_starting_services(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            initialized = self.run_controller("init", "--runtime-root", str(runtime_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)

            result = self.run_controller(
                "start",
                "--library-root",
                str(root / "missing-aarch64-libraries"),
                "--runtime-root",
                str(runtime_root),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("Runtime library root is not a directory", result.stderr)

    @ROOTLESS_ONLY
    def test_runtime_library_command_registers_one_shared_directory_without_copying(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            library_root = root / "target-libraries"
            library_root.mkdir()
            for name in ("libEGL.so", "libGLESv3.so", "libc.so"):
                header = bytearray(64)
                header[:4] = b"\x7fELF"
                header[4] = 2
                header[5] = 1
                header[18:20] = (183).to_bytes(2, "little")
                (library_root / name).write_bytes(bytes(header) + name.encode("utf-8"))

            result = self.run_controller(
                "runtime-libs",
                "--library-root",
                str(library_root),
                "--runtime-root",
                str(runtime_root),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            registry_path = runtime_root / "state" / "build-worker" / "runtime-library-source.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["library_root"], str(library_root.resolve()))
            self.assertFalse((runtime_root / "state" / "build-worker" / "profile-runtime-libraries").exists())

    def test_local_and_remote_modes_are_mutually_exclusive(self) -> None:
        result = self.run_controller("paths", "--local", "--remote")

        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be used together", result.stderr)

    @ROOTLESS_ONLY
    def test_paths_report_one_runtime_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            runtime_root = Path(temp) / "runtime"

            result = self.run_controller(
                "paths",
                "--runtime-root",
                str(runtime_root),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            reported = dict(
                line.split("=", 1)
                for line in result.stdout.splitlines()
                if "=" in line
            )
            self.assertEqual(reported["runtime_root"], str(runtime_root))
            for key in ("state", "assets", "logs", "run", "cache", "tmp", "env"):
                self.assertTrue(Path(reported[key]).is_relative_to(runtime_root), key)

    @ROOTLESS_ONLY
    def test_system_runtime_paths_are_rejected_before_write(self) -> None:
        result = self.run_controller(
            "paths",
            "--runtime-root",
            "/var/lib/heterogeneous-enable-web-test",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("must be a user-owned path", result.stderr)

    @ROOTLESS_ONLY
    def test_runtime_symlink_cannot_redirect_writes_outside_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            test_root = Path(temp)
            runtime_root = test_root / "runtime"
            outside_cache = test_root / "outside-cache"
            runtime_root.mkdir()
            outside_cache.mkdir()
            (runtime_root / "cache").symlink_to(outside_cache, target_is_directory=True)

            result = self.run_controller("init", "--runtime-root", str(runtime_root))

            self.assertEqual(result.returncode, 2)
            self.assertIn("must stay below runtime root", result.stderr)
            self.assertEqual(list(outside_cache.iterdir()), [])

    @ROOTLESS_ONLY
    def test_core_preflight_accepts_an_empty_optional_flag_list(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            runtime_root = Path(temp) / "runtime"
            init = self.run_controller("init", "--runtime-root", str(runtime_root))
            self.assertEqual(init.returncode, 0, init.stderr)

            result = self.run_controller("preflight", "--runtime-root", str(runtime_root))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("unbound variable", result.stderr)

    @ROOTLESS_ONLY
    @unittest.skipUnless(
        (REPOSITORY_ROOT / ".venv" / "bin" / "python").is_file()
        and (REPOSITORY_ROOT / "node_modules" / ".bin" / "next").is_file(),
        "dependency-reuse smoke test requires an installed development checkout",
    )
    def test_install_can_reuse_dependencies_without_building(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            runtime_root = Path(temp) / "runtime"

            result = self.run_controller(
                "install",
                "--runtime-root",
                str(runtime_root),
                "--skip-deps",
                "--skip-build",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Skipped the production Web build", result.stdout)
            self.assertTrue((runtime_root / "platform.env").is_file())
            self.assertTrue((runtime_root / "state").is_dir())

    @ROOTLESS_ONLY
    def test_stop_does_not_signal_a_process_from_a_stale_pid_record(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".deploy-test-", dir=REPOSITORY_ROOT) as temp:
            runtime_root = Path(temp) / "runtime"
            init = self.run_controller("init", "--runtime-root", str(runtime_root))
            self.assertEqual(init.returncode, 0, init.stderr)
            pid_file = runtime_root / "run" / "api.pid"
            pid_file.write_text(f"{os.getpid()} 0\n", encoding="utf-8")

            result = self.run_controller("stop", "--runtime-root", str(runtime_root))

            self.assertEqual(result.returncode, 0, result.stderr)
            os.kill(os.getpid(), 0)
            self.assertIn("refusing to stop", result.stderr)
            self.assertFalse(pid_file.exists())


if __name__ == "__main__":
    unittest.main()
