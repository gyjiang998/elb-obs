"""E0 standalone Build Worker.

The worker is deliberately separate from HDC.  It consumes an immutable staged
source artifact and a registered profile, compiles only inside a project-owned
temporary root, and emits a content-addressed build manifest.  Deploying that
manifest is a later DeviceExecutor responsibility.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

try:  # ``resource`` is unavailable on the Windows Bridge host.
    import resource
except ImportError:  # pragma: no cover - exercised by Windows deployments.
    resource = None  # type: ignore[assignment]

from .core import ArtifactStore, PlatformError, atomic_write_json, sha256_file, sha256_json
from .device_experiment import (
    EXPERIMENT_PROFILES,
    EXPERIMENT_ROOT,
    LLD_THREAD_COUNT,
    _compile_and_link,
    experiment_profile_catalog,
    prepare_profile_generated_sources,
    _profile,
    _staged_source_path,
    _toolchain,
)
from .platform_paths import glslang_binary
from .device_verification import _bounded, _clean_output
from .runtime_library_source import resolve_shared_profile_libraries


BUILD_LEVEL_E0 = "E0"
BUILD_RESOURCE_POLICY_ID = "build-worker-default/v3"
MAX_BUILD_TIMEOUT_SECONDS = 900
MAX_BUILD_LOG_BYTES = 128 * 1024
MAX_BUILD_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
MAX_BUILD_FILE_BYTES = 256 * 1024 * 1024
MAX_BUILD_WORKSPACE_BYTES = 512 * 1024 * 1024
MAX_BUILD_OPEN_FILES = 256
BUILD_PROCESS_LIMIT_ENFORCEMENT = "not_applied_shared_uid"


def _text(value: Any, limit: int = 512) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_relative(value: str) -> Path:
    candidate = Path(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        raise PlatformError("BUILD_ARTIFACT_INVALID", "构建产物路径不在受控目录内。", status_code=422)
    return candidate


def _enabled_environment_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


class BuildWorker:
    """A profile-owned build service with deterministic cache identity."""

    def __init__(self, *, workspace_root: Path, state_root: Path, artifact_store: ArtifactStore) -> None:
        self.workspace_root = workspace_root.resolve()
        self.state_root = state_root.resolve()
        self.artifact_store = artifact_store
        self.root = self.state_root / "build-worker"
        self.cache_root = self.root / "cache"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._thread_context = threading.local()
        self._active_processes: dict[str, set[subprocess.Popen[str]]] = {}
        self._cancelled_runs: set[str] = set()

    def _toolchain_identity(self) -> dict[str, Any]:
        try:
            clang, clangxx, lld = _toolchain(self.workspace_root)
        except PlatformError as exc:
            return {"status": "unavailable", "error_code": exc.code, "message": exc.message}
        files = {"clang": clang, "clangxx": clangxx, "ld_lld": lld}
        return {
            "status": "available",
            "files": {name: {"name": path.name, "sha256": sha256_file(path)} for name, path in files.items()},
            "shader_compiler": (
                {
                    "name": glslang_binary().name,
                    "sha256": sha256_file(glslang_binary()),
                }
                if glslang_binary().is_file()
                else {"status": "unavailable"}
            ),
        }

    def _profile_version(self, profile_id: str) -> str:
        profile = _profile(profile_id)
        return sha256_json(
            {
                "profile_id": profile_id,
                "unit": profile["unit"],
                "language": profile["language"],
                "compiler": profile["compiler"],
                "target": profile["target"],
                "standard": profile["standard"],
                "include_dirs": list(profile["include_dirs"]),
                "libraries": list(profile["libraries"]),
                "link_mode": profile["link_mode"],
                "source_entrypoint": profile.get("source_entrypoint"),
                "runtime_harness": "build_worker_startup_shim/v1" if profile.get("source_entrypoint") else "source_owned",
                "generated_headers": list(profile.get("generated_headers") or ()),
                "shader_contract": profile.get("shader_contract"),
            }
        )

    def _legacy_runtime_library_root(self, profile_id: str) -> Path:
        configured = os.environ.get("HETERO_BUILD_PROFILE_LIBRARY_ROOT", "").strip()
        base = Path(configured).expanduser().resolve() if configured else (self.root / "profile-runtime-libraries").resolve()
        return base / profile_id

    @staticmethod
    def _runtime_library_identity(value: dict[str, Any]) -> dict[str, Any]:
        """Keep cache identity content-based instead of host-path-based."""

        return {
            "source": _text(value.get("source"), 80),
            "bundle_sha256": _text(value.get("bundle_sha256"), 80),
            "profile_id": _text(value.get("profile_id"), 80),
            "libraries": [
                {
                    "name": _text(_record(item).get("name"), 160),
                    "sha256": _text(_record(item).get("sha256"), 80),
                }
                for item in value.get("libraries", [])
                if isinstance(item, dict)
            ],
            "error_code": _text(_record(value.get("error")).get("code"), 120),
        }

    def _network_sandbox_policy(self) -> tuple[list[str], dict[str, Any]]:
        """Resolve an administrator-owned, shell-free network sandbox wrapper.

        A model never supplies this value.  Production deployments set
        ``HETERO_BUILD_REQUIRE_NETWORK_SANDBOX=1`` plus an administrator-owned
        wrapper such as a Bubblewrap/AppContainer launcher.  Local development
        intentionally remains usable without such a launcher, but its manifest
        reports that network isolation was not hard-enforced instead of making
        a false security claim.
        """

        raw_prefix = os.environ.get("HETERO_BUILD_SANDBOX_PREFIX", "").strip()
        required = _enabled_environment_flag("HETERO_BUILD_REQUIRE_NETWORK_SANDBOX")
        attested = _enabled_environment_flag("HETERO_BUILD_SANDBOX_NETWORK_ISOLATED")
        if not raw_prefix:
            if required:
                raise PlatformError(
                    "BUILD_NETWORK_SANDBOX_UNAVAILABLE",
                    "当前部署要求 Build Worker 网络隔离，但未配置管理员管理的 HETERO_BUILD_SANDBOX_PREFIX。",
                    status_code=503,
                    details={
                        "required": True,
                        "next_action_zh": "由平台管理员配置不经 shell 的网络隔离 wrapper，并显式声明 HETERO_BUILD_SANDBOX_NETWORK_ISOLATED=1。",
                    },
                )
            return [], {
                "network": "not_hard_enforced_development",
                "network_isolation_required": False,
                "wrapper_configured": False,
            }
        try:
            prefix = shlex.split(raw_prefix, posix=os.name != "nt")
        except ValueError as exc:
            raise PlatformError("BUILD_SANDBOX_CONFIGURATION_INVALID", "Build Worker 网络隔离 wrapper 配置无法解析。", status_code=503) from exc
        if not prefix or any("\x00" in item for item in prefix):
            raise PlatformError("BUILD_SANDBOX_CONFIGURATION_INVALID", "Build Worker 网络隔离 wrapper 配置无效。", status_code=503)
        executable = Path(prefix[0]).expanduser()
        if executable.is_absolute() and not executable.is_file():
            raise PlatformError(
                "BUILD_SANDBOX_CONFIGURATION_INVALID",
                "管理员配置的 Build Worker 网络隔离 wrapper 不存在。",
                status_code=503,
                details={"wrapper": executable.name},
            )
        if required and not attested:
            raise PlatformError(
                "BUILD_NETWORK_SANDBOX_UNATTESTED",
                "当前部署要求 Build Worker 网络隔离，但管理员尚未确认 wrapper 隔离网络。",
                status_code=503,
                details={
                    "next_action_zh": "确认 wrapper 已创建独立网络命名空间或等效隔离后设置 HETERO_BUILD_SANDBOX_NETWORK_ISOLATED=1。",
                },
            )
        return prefix, {
            "network": "hard_enforced_by_admin_wrapper" if attested else "wrapper_configured_not_attested",
            "network_isolation_required": required,
            "wrapper_configured": True,
            "wrapper_identity": sha256_json(prefix),
        }

    def describe_profile(self, *, profile_id: str = "", header_query: str = "") -> list[dict[str, Any]]:
        """Expose the fixed build surface as a typed backend fact.

        This is deliberately separate from the optimization/hardware KB.  It
        answers which headers and linked runtime surface the E0 worker can
        actually use; it does not claim that a connected device supports a
        given accelerator or that a generated experiment will succeed.
        """

        normalized_profile = _text(profile_id, 80)
        if normalized_profile:
            _profile(normalized_profile)
        profiles = experiment_profile_catalog(
            self.workspace_root,
            state_root=self.state_root,
            profile_id=normalized_profile or None,
            header_query=_text(header_query, 120),
        )
        for profile in profiles:
            candidate_id = _text(profile.get("profile_id"), 80)
            profile["runtime_library_surface"] = self._runtime_library_surface(candidate_id)
        return profiles

    def _runtime_library_surface(self, profile_id: str) -> dict[str, Any]:
        """Report whether ``build.create`` can use a profile before staging code."""

        try:
            _, manifest = self._fixed_profile_libraries(profile_id)
        except PlatformError as exc:
            details = _record(exc.details)
            return {
                "status": "unavailable",
                "build_create_ready": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": {
                        key: details[key]
                        for key in (
                            "profile_id",
                            "expected_library_names",
                            "missing_library_names",
                            "bundle_manifest_present",
                            "mismatched_library_names",
                            "runtime_library_source",
                            "configured_root",
                            "registry_path",
                            "next_action_zh",
                        )
                        if key in details
                    },
                },
            }
        return {
            "status": "ready",
            "build_create_ready": True,
            "source": manifest.get("source"),
            "bundle_sha256": manifest.get("bundle_sha256"),
            "libraries": manifest.get("libraries", []),
        }

    def _fixed_profile_libraries(self, profile_id: str) -> tuple[list[Path], dict[str, Any]]:
        profile = _profile(profile_id)
        if profile["link_mode"] == "static":
            return [], {"source": "not_required", "profile_id": profile_id, "libraries": []}
        shared = resolve_shared_profile_libraries(state_root=self.state_root, profile_id=profile_id)
        if shared is not None:
            return shared

        # Compatibility only: installations created before the shared-root
        # contract may still have one copied bundle per profile.  New
        # deployments should use runtime-library-source.json or
        # HETERO_BUILD_RUNTIME_LIBRARY_ROOT instead.
        library_root = self._legacy_runtime_library_root(profile_id)
        expected = [name for _, name in profile["libraries"]]
        paths = [library_root / name for name in expected]
        missing = [path.name for path in paths if not path.is_file()]
        bundle_path = library_root / "bundle.json"
        if missing or not bundle_path.is_file():
            raise PlatformError(
                "BUILD_PROFILE_RUNTIME_UNAVAILABLE",
                "后端 Build Worker 尚未配置该 profile 的固定运行时库，不能从手机临时拉取库后再伪称可复现构建。",
                status_code=503,
                details={
                    "profile_id": profile_id,
                    "expected_library_names": expected,
                    "missing_library_names": missing,
                    "bundle_manifest_present": bundle_path.is_file(),
                    "configured_root": str(library_root),
                    "next_action_zh": "由平台管理员登记一个包含目标 AArch64 OpenHarmony 运行库的共享目录；无需为每个 profile 复制一套库。",
                },
            )
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlatformError("BUILD_PROFILE_RUNTIME_INVALID", "固定运行时库 bundle.json 不可读取。", status_code=503, details={"profile_id": profile_id}) from exc
        if not isinstance(bundle, dict) or bundle.get("schema_version") != "build_profile_runtime_bundle/v1" or bundle.get("profile_id") != profile_id:
            raise PlatformError("BUILD_PROFILE_RUNTIME_INVALID", "固定运行时库 bundle 的 profile 或 schema 不匹配。", status_code=503, details={"profile_id": profile_id})
        declared = {
            _text(_record(item).get("name"), 160): _text(_record(item).get("sha256"), 80)
            for item in bundle.get("libraries", [])
            if isinstance(item, dict)
        }
        mismatched = [path.name for path in paths if not declared.get(path.name) or declared[path.name] != sha256_file(path)]
        if mismatched:
            raise PlatformError(
                "BUILD_PROFILE_RUNTIME_INVALID",
                "固定运行时库 bundle 的文件哈希与登记清单不一致。",
                status_code=409,
                details={"profile_id": profile_id, "mismatched_library_names": mismatched},
            )
        return paths, {
            "source": "legacy_fixed_profile_bundle",
            "profile_id": profile_id,
            "root": str(library_root),
            "bundle_sha256": sha256_file(bundle_path),
            "libraries": [{"name": path.name, "sha256": sha256_file(path)} for path in paths],
        }

    @staticmethod
    def _snapshot_runtime_libraries(
        run_root: Path,
        libraries: list[Path],
        library_manifest: dict[str, Any],
    ) -> tuple[list[Path], list[dict[str, Any]]]:
        """Copy hash-pinned link inputs into the content-addressed build root."""

        declared = {
            _text(_record(item).get("name"), 160): _text(_record(item).get("sha256"), 80)
            for item in library_manifest.get("libraries", [])
            if isinstance(item, dict)
        }
        snapshots: list[Path] = []
        bundle_members: list[dict[str, Any]] = []
        for source in libraries:
            expected_hash = declared.get(source.name)
            if not expected_hash:
                raise PlatformError(
                    "BUILD_PROFILE_RUNTIME_INVALID",
                    "运行库清单缺少本次链接输入的哈希。",
                    status_code=409,
                    details={"missing_library_names": [source.name]},
                )
            destination = run_root / "lib" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            observed_hash = sha256_file(destination)
            if observed_hash != expected_hash:
                raise PlatformError(
                    "BUILD_PROFILE_RUNTIME_CHANGED",
                    "共享运行库在构建快照期间发生变化，已停止本次链接。",
                    status_code=409,
                    details={
                        "mismatched_library_names": [source.name],
                        "next_action_zh": "确认管理员运行库目录稳定后重试；若是有意升级，请重新登记共享运行库根目录。",
                    },
                )
            snapshots.append(destination)
            bundle_members.append({
                "relative_path": f"lib/{source.name}",
                "sha256": observed_hash,
                "bytes": destination.stat().st_size,
            })
        return snapshots, bundle_members

    @staticmethod
    def _phase_diagnostic(value: Any) -> dict[str, Any] | None:
        phase = _record(value)
        if not phase:
            return None
        command = phase.get("command") if isinstance(phase.get("command"), list) else []
        return {
            "return_code": phase.get("return_code"),
            "command": [_text(argument, 1024) for argument in command[:96]],
            "stdout": _text(phase.get("stdout"), 4096),
            "stderr": _text(phase.get("stderr"), 16 * 1024),
        }

    @classmethod
    def _build_diagnostic(cls, manifest: dict[str, Any]) -> dict[str, Any]:
        """Project a bounded, actionable build observation for the ReAct loop."""

        error = _record(manifest.get("error"))
        logs = _record(manifest.get("logs"))
        shader_compile_detail = cls._phase_diagnostic(logs.get("shader_compile"))
        compile_detail = cls._phase_diagnostic(logs.get("compile"))
        link_detail = cls._phase_diagnostic(logs.get("link"))
        error_code = _text(error.get("code"), 120)
        if error_code.startswith("BUILD_PROFILE_RUNTIME"):
            stage = "profile_runtime"
        elif error_code.startswith("BUILD_TOOLCHAIN") or error_code.startswith("DEVICE_EXPERIMENT_TOOLCHAIN"):
            stage = "toolchain"
        elif error_code.startswith("BUILD_NETWORK_SANDBOX"):
            stage = "sandbox"
        elif error_code.startswith("BUILD_SHADER") or shader_compile_detail and shader_compile_detail.get("return_code") not in (None, 0):
            stage = "shader_compile"
        elif compile_detail and compile_detail.get("return_code") not in (None, 0):
            stage = "compile"
        elif link_detail and link_detail.get("return_code") not in (None, 0):
            stage = "link"
        elif manifest.get("status") == "succeeded":
            stage = "completed"
        else:
            stage = "unknown"
        projected_error: dict[str, Any] | None
        if error:
            projected_error = {
                "code": error_code,
                "message": _text(error.get("message"), 2400),
                "details": _record(error.get("details")),
            }
        elif stage in {"compile", "link"}:
            phase = compile_detail if stage == "compile" else link_detail
            assert phase is not None
            stderr = _text(phase.get("stderr"), 16 * 1024)
            normalized_stderr = stderr.casefold()
            linker_thread_unavailable = stage == "link" and (
                "thread constructor failed" in normalized_stderr
                or (
                    "resource temporarily unavailable" in normalized_stderr
                    and ("ld.lld" in normalized_stderr or "libc++abi" in normalized_stderr)
                )
            )
            if linker_thread_unavailable:
                projected_error = {
                    "code": "BUILD_LINK_RESOURCE_UNAVAILABLE",
                    "message": (
                        "链接器无法创建工作线程，属于 Build Worker 宿主资源或进程限制问题；"
                        "修改实验源码不能修复该故障。"
                    ),
                    "details": {
                        "return_code": phase.get("return_code"),
                        "remediation_kind": "build_worker_host_resource",
                        "source_rewrite_recommended": False,
                        "retry_policy": "retry_after_worker_capacity_recovers",
                        "observed_stderr": _text(stderr, 4096),
                    },
                }
            else:
                projected_error = {
                    "code": "BUILD_COMPILE_FAILED" if stage == "compile" else "BUILD_LINK_FAILED",
                    "message": _text(stderr, 2400) or f"{stage} 返回非零状态。",
                    "details": {"return_code": phase.get("return_code")},
                }
        else:
            projected_error = None
        return {
            "stage": stage,
            "error": projected_error,
            "shader_compile": shader_compile_detail,
            "compile": compile_detail,
            "link": link_detail,
        }

    @classmethod
    def _build_summary(cls, manifest: dict[str, Any]) -> str:
        if manifest.get("status") == "succeeded":
            return "E0 Build Worker 已生成可部署 manifest；构建成功不等价于手机可用、功能等价、硬件参与或性能收益。"
        diagnostic = cls._build_diagnostic(manifest)
        error = _record(diagnostic.get("error"))
        if error:
            return f"E0 构建失败 [{_text(error.get('code'), 120) or 'BUILD_FAILED'}]：{_text(error.get('message'), 1200) or '构建环境不可用。'}"
        phase = _record(diagnostic.get(str(diagnostic.get("stage"))))
        stderr = _text(phase.get("stderr"), 1200)
        if stderr:
            return f"E0 {diagnostic.get('stage')} 失败：{stderr}"
        return "E0 Build Worker 未形成可部署二进制；构建诊断不足。"

    def _build_identity(
        self,
        *,
        staged_source: dict[str, Any],
        profile_id: str,
        target: dict[str, Any],
        toolchain: dict[str, Any],
        runtime_libraries: dict[str, Any],
        sandbox_policy: dict[str, Any],
    ) -> str:
        return sha256_json(
            {
                "schema_version": "build_request/v1",
                "source_sha256": _text(staged_source.get("source_sha256"), 80),
                "auxiliary_sources": staged_source.get("auxiliary_sources") if isinstance(staged_source.get("auxiliary_sources"), dict) else {},
                "profile_id": profile_id,
                "profile_version": self._profile_version(profile_id),
                "toolchain": toolchain,
                "runtime_libraries": self._runtime_library_identity(runtime_libraries),
                "target": target,
                "build_level": BUILD_LEVEL_E0,
                "resource_policy_id": BUILD_RESOURCE_POLICY_ID,
                "network_sandbox": {
                    "network": _text(sandbox_policy.get("network"), 120),
                    "wrapper_identity": _text(sandbox_policy.get("wrapper_identity"), 80),
                },
            }
        )

    def _build_environment(self) -> dict[str, str]:
        # Do not pass developer-specific loader, proxy or compiler variables to
        # an Agent-authored compilation.  PATH is only used by the dynamic
        # loader of the fixed toolchain binary; compiler paths are absolute.
        allowed = {key: os.environ[key] for key in ("PATH", "TMPDIR", "TEMP", "TMP") if key in os.environ}
        allowed["LANG"] = "C"
        allowed["LC_ALL"] = "C"
        allowed["NO_PROXY"] = "*"
        return allowed

    @staticmethod
    def _preexec_limits() -> None:
        # This hook is POSIX only; Windows uses the same profile/argv boundary
        # and records that hard rlimits are unavailable in that deployment.
        if resource is None:
            return
        # RLIMIT_NPROC is accounted against the process real UID on Linux, not
        # against this child tree. A low value therefore makes lld fail when a
        # shared service account already owns unrelated processes or threads.
        limits = (
            (resource.RLIMIT_CPU, (MAX_BUILD_TIMEOUT_SECONDS, MAX_BUILD_TIMEOUT_SECONDS + 5)),
            (resource.RLIMIT_AS, (MAX_BUILD_MEMORY_BYTES, MAX_BUILD_MEMORY_BYTES)),
            (resource.RLIMIT_FSIZE, (MAX_BUILD_FILE_BYTES, MAX_BUILD_FILE_BYTES)),
            (resource.RLIMIT_NOFILE, (MAX_BUILD_OPEN_FILES, MAX_BUILD_OPEN_FILES)),
        )
        for limit_kind, value in limits:
            try:
                resource.setrlimit(limit_kind, value)
            except (ValueError, OSError):
                continue

    def _run_process(self, command: list[str], *, timeout_seconds: int, cwd: Path, **_: Any) -> subprocess.CompletedProcess[str]:
        run_id = _text(getattr(self._thread_context, "agent_run_id", ""), 120)
        sandbox_prefix = getattr(self._thread_context, "sandbox_prefix", [])
        effective_command = [*sandbox_prefix, *command]
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                effective_command,
                cwd=str(cwd),
                env=self._build_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                preexec_fn=self._preexec_limits if os.name == "posix" else None,
                start_new_session=os.name == "posix",
            )
            if run_id:
                with self._lock:
                    self._active_processes.setdefault(run_id, set()).add(process)
            stdout, stderr = process.communicate(timeout=min(timeout_seconds, MAX_BUILD_TIMEOUT_SECONDS))
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                self._terminate_process(process)
                stdout, stderr = process.communicate()
            else:
                stdout, stderr = str(exc.stdout or ""), str(exc.stderr or "")
            raise PlatformError(
                "BUILD_WORKER_TIMEOUT",
                "Build Worker 在资源策略允许的时间内没有完成。",
                status_code=504,
                details={
                    "timeout_seconds": min(timeout_seconds, MAX_BUILD_TIMEOUT_SECONDS),
                    "stdout": _bounded(str(stdout or ""), 4096),
                    "stderr": _bounded(str(stderr or ""), 4096),
                },
            ) from exc
        except OSError as exc:
            raise PlatformError(
                "BUILD_WORKER_TOOLCHAIN_FAILED",
                "Build Worker 无法启动固定工具链进程。",
                status_code=503,
                details={"reason": str(exc)[:600]},
            ) from exc
        finally:
            if run_id and process is not None:
                with self._lock:
                    active = self._active_processes.get(run_id)
                    if active is not None:
                        active.discard(process)
                        if not active:
                            self._active_processes.pop(run_id, None)
        if process is None:
            raise PlatformError("BUILD_WORKER_FAILED", "Build Worker 未能启动固定工具链进程。", status_code=500)
        if run_id and run_id in self._cancelled_runs:
            raise PlatformError("BUILD_WORKER_CANCELLED", "用户已取消该 Agent Run 的后端编译任务。", status_code=499)
        return subprocess.CompletedProcess(
            effective_command,
            process.returncode,
            _bounded(_clean_output(stdout), MAX_BUILD_LOG_BYTES),
            _bounded(_clean_output(stderr), MAX_BUILD_LOG_BYTES),
        )

    @staticmethod
    def _directory_size(root: Path) -> int:
        total = 0
        for candidate in root.rglob("*"):
            try:
                if candidate.is_file():
                    total += candidate.stat().st_size
            except OSError:
                continue
            if total > MAX_BUILD_WORKSPACE_BYTES:
                return total
        return total

    @classmethod
    def _enforce_workspace_size(cls, root: Path) -> None:
        observed = cls._directory_size(root)
        if observed > MAX_BUILD_WORKSPACE_BYTES:
            raise PlatformError(
                "BUILD_WORKSPACE_LIMIT",
                "Build Worker 临时工作目录超过磁盘资源上限。",
                status_code=413,
                details={"observed_bytes": observed, "max_workspace_bytes": MAX_BUILD_WORKSPACE_BYTES},
            )

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:  # pragma: no cover - exercised by a Windows Build Worker deployment.
                process.terminate()
        except (OSError, ProcessLookupError):
            return

    def cancel_run(self, agent_run_id: str) -> None:
        """Stop active fixed-toolchain children for a cancelled Agent Run."""

        run_id = _text(agent_run_id, 120)
        if not run_id:
            return
        with self._lock:
            self._cancelled_runs.add(run_id)
            active = list(self._active_processes.get(run_id, set()))
        for process in active:
            self._terminate_process(process)

    @staticmethod
    def _write_bundle_text(path: Path, content: str) -> None:
        """Write a bounded package member without exposing a model path."""

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _materialize_bundle_metadata(self, run_root: Path, manifest: dict[str, Any]) -> None:
        """Materialize the E0 package layout declared by ``build_manifest/v1``.

        The manifest is still the deployment authority.  These files make a
        cached result inspectable and portable without turning arbitrary build
        paths into part of the Agent contract.
        """

        run = _record(manifest.get("run"))
        if run:
            atomic_write_json(run_root / "run" / "experiment.json", run)
        logs = _record(manifest.get("logs"))
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        for phase in ("shader_compile", "compile", "link"):
            detail = _record(logs.get(phase))
            stdout = _text(detail.get("stdout"), MAX_BUILD_LOG_BYTES)
            stderr = _text(detail.get("stderr"), MAX_BUILD_LOG_BYTES)
            if stdout:
                stdout_parts.append(f"[{phase}]\n{stdout}\n")
            if stderr:
                stderr_parts.append(f"[{phase}]\n{stderr}\n")
        error = _record(manifest.get("error"))
        if error:
            stderr_parts.append(f"[build-worker]\n{json.dumps(error, ensure_ascii=False, separators=(',', ':'))}\n")
        self._write_bundle_text(run_root / "logs" / "build.stdout", "".join(stdout_parts))
        self._write_bundle_text(run_root / "logs" / "build.stderr", "".join(stderr_parts))

    def stage_source(
        self,
        *,
        project_id: str,
        source_connector_id: str,
        source_binding: dict[str, Any],
        profile_id: str,
        language: str,
        title: str,
        source: str,
        agent_run_id: str,
        shader_source: str = "",
    ) -> dict[str, Any]:
        """Stage source with the existing fixed-root header validation path."""

        # Existing staging performs the important include-root validation.  A
        # synthetic, unbound probe makes source staging independent of a USB
        # device; the actual build request binds a real probe when required.
        from .device_experiment import stage_device_experiment_source

        staged = stage_device_experiment_source(
            self.workspace_root,
            self.state_root,
            probe_artifact={"artifact_id": "unbound_build_source", "device": {"serial": "unbound"}},
            profile_id=profile_id,
            title=title,
            language=language,
            source=source,
            shader_source=shader_source,
        )
        staged["schema_version"] = "build_source/v1"
        staged["agent_run_id"] = agent_run_id
        staged["probe_artifact_id"] = None
        staged["summary_zh"] = "Build Worker 已在固定 profile 的 SDK/header 根下暂存并校验源码；尚未关联设备、编译或部署。"
        return self.artifact_store.write(
            "build_source",
            staged,
            source_connector_id=source_connector_id,
            source_binding=source_binding,
            producer={"name": "build-worker", "version": "1.0.0"},
            input_artifacts=[],
        )

    def build(
        self,
        *,
        project_id: str,
        agent_run_id: str,
        source_artifact: dict[str, Any],
        profile_id: str,
        target: dict[str, Any],
        timeout_seconds: int = 240,
    ) -> dict[str, Any]:
        """Create an E0 build request and its deterministic result artifact."""

        if source_artifact.get("project_id") != project_id:
            raise PlatformError("BUILD_SOURCE_PROJECT_MISMATCH", "构建源码不属于当前项目。", status_code=409)
        source_profile = _text(source_artifact.get("profile_id"), 80)
        if source_profile != profile_id:
            raise PlatformError("BUILD_PROFILE_MISMATCH", "构建请求的 profile 与暂存源码不一致。", status_code=409)
        if source_artifact.get("schema_version") not in {"build_source/v1", "agent_device_experiment_source/v1"}:
            raise PlatformError("BUILD_SOURCE_INVALID", "构建请求必须引用 Build Worker 暂存的源码工件。", status_code=422)
        profile = _profile(profile_id)
        target_normalized = {
            "os": _text(target.get("os"), 80) or "OpenHarmony",
            "abi": _text(target.get("abi"), 80) or "arm64",
            "api_level": _text(target.get("api_level"), 80) or "observed-or-unknown",
            "device_probe_artifact_id": _text(target.get("device_probe_artifact_id"), 120) or None,
        }
        if target_normalized["abi"] != "arm64":
            raise PlatformError("BUILD_TARGET_UNSUPPORTED", "当前 E0 Build Worker 仅支持 OpenHarmony arm64。", status_code=422)
        toolchain = self._toolchain_identity()
        runtime_library_error: PlatformError | None = None
        try:
            libraries, library_manifest = self._fixed_profile_libraries(profile_id)
        except PlatformError as exc:
            runtime_library_error = exc
            libraries = []
            library_manifest = {
                "source": "unavailable",
                "profile_id": profile_id,
                "libraries": [],
                "error": {"code": exc.code, "message": exc.message},
            }
        sandbox_error: PlatformError | None = None
        try:
            sandbox_prefix, sandbox_policy = self._network_sandbox_policy()
        except PlatformError as exc:
            sandbox_error = exc
            sandbox_prefix = []
            sandbox_policy = {
                "network": "required_but_unavailable",
                "network_isolation_required": True,
                "wrapper_configured": False,
                "error": {"code": exc.code, "message": exc.message},
            }
        identity = self._build_identity(
            staged_source=source_artifact,
            profile_id=profile_id,
            target=target_normalized,
            toolchain=toolchain,
            runtime_libraries=library_manifest,
            sandbox_policy=sandbox_policy,
        )
        request_payload = {
            "schema_version": "build_request/v1",
            "project_id": project_id,
            "agent_run_id": agent_run_id,
            "source_artifact_id": source_artifact.get("artifact_id"),
            "source_sha256": source_artifact.get("source_sha256"),
            "build_level": BUILD_LEVEL_E0,
            "profile_id": profile_id,
            "profile_version": self._profile_version(profile_id),
            "target": target_normalized,
            "resource_policy_id": BUILD_RESOURCE_POLICY_ID,
            "toolchain": toolchain,
            "runtime_libraries": self._runtime_library_identity(library_manifest),
            "network_sandbox": {
                "network": sandbox_policy.get("network"),
                "wrapper_identity": sandbox_policy.get("wrapper_identity"),
            },
            "idempotency_key": identity,
        }
        request_artifact = self.artifact_store.write(
            "build_request",
            request_payload,
            source_connector_id=str(source_artifact["source_connector_id"]),
            source_binding=_record(source_artifact.get("source_binding")),
            producer={"name": "build-worker", "version": "1.0.0"},
            input_artifacts=[f"artifact:{source_artifact['artifact_id']}"],
        )
        run_root = self.cache_root / identity
        manifest_path = run_root / "manifest.json"
        with self._lock:
            if manifest_path.is_file():
                try:
                    cached_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    cached_manifest = {}
                if cached_manifest.get("build_identity") == identity:
                    result_payload = {
                        "schema_version": "build_result/v1",
                        "build_request_artifact_id": request_artifact["artifact_id"],
                        "build_identity": identity,
                        "cache_hit": True,
                        "status": cached_manifest.get("status", "unknown"),
                        "manifest": cached_manifest,
                        "diagnostic": self._build_diagnostic(cached_manifest),
                        "summary_zh": f"Build Worker 命中缓存。{self._build_summary(cached_manifest)}",
                    }
                    return self.artifact_store.write(
                        "build_result",
                        result_payload,
                        source_connector_id=str(source_artifact["source_connector_id"]),
                        source_binding=_record(source_artifact.get("source_binding")),
                        producer={"name": "build-worker", "version": "1.0.0"},
                        input_artifacts=[f"artifact:{request_artifact['artifact_id']}", f"artifact:{source_artifact['artifact_id']}"],
                    )
            if run_root.exists():
                # A partial prior build has no successful manifest identity;
                # retain it for diagnosis and use a fresh work directory.
                run_root = self.cache_root / f"{identity}-{secrets.token_hex(4)}"
                manifest_path = run_root / "manifest.json"
            run_root.mkdir(parents=True, exist_ok=False)

        started = time.monotonic()
        source_path = _staged_source_path(self.state_root, source_artifact)
        source_copy = run_root / ("source.cc" if profile["language"] == "cpp" else "source.c")
        shutil.copyfile(source_path, source_copy)
        if sha256_file(source_copy) != _text(source_artifact.get("source_sha256"), 80):
            raise PlatformError("BUILD_SOURCE_HASH_MISMATCH", "Build Worker 复制源码后哈希不匹配。", status_code=500)
        self._thread_context.agent_run_id = agent_run_id
        self._thread_context.sandbox_prefix = sandbox_prefix
        with self._lock:
            self._cancelled_runs.discard(agent_run_id)
        build_libraries = libraries
        bundled_libraries: list[dict[str, Any]] = []
        try:
            if sandbox_error is not None:
                raise sandbox_error
            if runtime_library_error is not None:
                raise runtime_library_error
            if toolchain.get("status") != "available":
                raise PlatformError("BUILD_TOOLCHAIN_UNAVAILABLE", "固定 LLVM 工具链不可用。", status_code=503, details=toolchain)
            build_libraries, bundled_libraries = self._snapshot_runtime_libraries(
                run_root,
                libraries,
                library_manifest,
            )
            generated = prepare_profile_generated_sources(
                self.state_root,
                run_root,
                source_artifact,
                profile,
                run_process=self._run_process,
                timeout_seconds=timeout_seconds,
            )
            binary, detail = _compile_and_link(
                self.workspace_root,
                self.state_root,
                run_root,
                source_copy,
                profile_id,
                profile,
                build_libraries,
                run_process=self._run_process,
            )
            detail.update(generated)
            status = "succeeded" if binary is not None else "compile_failed"
            binary_record: dict[str, Any] | None = None
            if binary is not None:
                bundle_binary = run_root / "bin" / "experiment.bin"
                bundle_binary.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(binary), bundle_binary)
                binary_record = {
                    "relative_path": "bin/experiment.bin",
                    "sha256": sha256_file(bundle_binary),
                    "bytes": bundle_binary.stat().st_size,
                }
                # Dynamic inputs were already copied and hash-verified before
                # linking.  The same immutable snapshots are retained in the
                # build package for audit and offline inspection.
                self._enforce_workspace_size(run_root)
            manifest = {
                "schema_version": "build_manifest/v1",
                "build_identity": identity,
                "status": status,
                "build_level": BUILD_LEVEL_E0,
                "profile_id": profile_id,
                "profile_version": self._profile_version(profile_id),
                "target": target_normalized,
                "toolchain": toolchain,
                "source": {
                    "sha256": source_artifact.get("source_sha256"),
                    "relative_path": "source.c" if profile["language"] == "c" else "source.cc",
                    "auxiliary_sources": source_artifact.get("auxiliary_sources") if isinstance(source_artifact.get("auxiliary_sources"), dict) else {},
                },
                "libraries": {**library_manifest, "bundle_members": bundled_libraries},
                "binary": binary_record,
                "run": {
                    "entrypoint": "_start",
                    "source_entrypoint": str(profile.get("source_entrypoint") or "_start"),
                    "runtime_harness": "build_worker_startup_shim/v1" if profile.get("source_entrypoint") else "source_owned",
                    "arguments": [],
                    "environment": {},
                    "timeout_seconds": min(timeout_seconds, MAX_BUILD_TIMEOUT_SECONDS),
                    "expected_output_type": "experiment_defined_text",
                    "cpu_reference_comparison": "implemented_by_experiment_source",
                    "cleanup_policy": "device_job_owned_remote_directory",
                },
                "logs": {
                    **detail,
                    "stdout_relative_path": "logs/build.stdout",
                    "stderr_relative_path": "logs/build.stderr",
                },
                "resource_policy": {
                    "id": BUILD_RESOURCE_POLICY_ID,
                    "network": sandbox_policy.get("network"),
                    "network_isolation_required": bool(sandbox_policy.get("network_isolation_required")),
                    "network_sandbox_wrapper_configured": bool(sandbox_policy.get("wrapper_configured")),
                    "network_sandbox_wrapper_identity": sandbox_policy.get("wrapper_identity"),
                    "max_timeout_seconds": MAX_BUILD_TIMEOUT_SECONDS,
                    "max_memory_bytes": MAX_BUILD_MEMORY_BYTES,
                    "max_processes": None,
                    "process_limit_enforcement": BUILD_PROCESS_LIMIT_ENFORCEMENT,
                    "linker_threads": LLD_THREAD_COUNT,
                    "max_file_bytes": MAX_BUILD_FILE_BYTES,
                    "max_workspace_bytes": MAX_BUILD_WORKSPACE_BYTES,
                    "max_open_files": MAX_BUILD_OPEN_FILES,
                    "environment_allowlist": sorted(self._build_environment()),
                    "platform_rlimit_enforced": os.name == "posix",
                    "enforced_rlimits": (
                        ["cpu_seconds", "address_space", "file_size", "open_files"]
                        if os.name == "posix"
                        else []
                    ),
                },
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
        except PlatformError as exc:
            error_details = _record(exc.details)
            shader_compile = _record(error_details.get("shader_compile"))
            manifest = {
                "schema_version": "build_manifest/v1",
                "build_identity": identity,
                "status": "failed",
                "build_level": BUILD_LEVEL_E0,
                "profile_id": profile_id,
                "target": target_normalized,
                "toolchain": toolchain,
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                **({"logs": {"shader_compile": shader_compile}} if shader_compile else {}),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
        finally:
            self._thread_context.agent_run_id = ""
            self._thread_context.sandbox_prefix = []
        self._materialize_bundle_metadata(run_root, manifest)
        atomic_write_json(manifest_path, manifest)
        result_payload = {
            "schema_version": "build_result/v1",
            "build_request_artifact_id": request_artifact["artifact_id"],
            "build_identity": identity,
            "cache_hit": False,
            "status": manifest["status"],
            "manifest": manifest,
            "diagnostic": self._build_diagnostic(manifest),
            "bundle_relative_path": str(manifest_path.parent.relative_to(self.state_root)),
            "summary_zh": self._build_summary(manifest),
        }
        return self.artifact_store.write(
            "build_result",
            result_payload,
            source_connector_id=str(source_artifact["source_connector_id"]),
            source_binding=_record(source_artifact.get("source_binding")),
            producer={"name": "build-worker", "version": "1.0.0"},
            input_artifacts=[f"artifact:{request_artifact['artifact_id']}", f"artifact:{source_artifact['artifact_id']}"],
        )

    def binary_path(self, build_result: dict[str, Any]) -> Path:
        manifest = _record(build_result.get("manifest"))
        binary = _record(manifest.get("binary"))
        relative_root = _safe_relative(_text(build_result.get("bundle_relative_path"), 512))
        relative_binary = _safe_relative(_text(binary.get("relative_path"), 256))
        candidate = (self.state_root / relative_root / relative_binary).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PlatformError("BUILD_ARTIFACT_INVALID", "构建二进制不在 Build Worker 管理目录内。", status_code=422) from exc
        expected = _text(binary.get("sha256"), 80)
        if not candidate.is_file() or not expected or sha256_file(candidate) != expected:
            raise PlatformError("BUILD_ARTIFACT_CHANGED", "构建二进制不存在或哈希已变化。", status_code=409)
        return candidate
