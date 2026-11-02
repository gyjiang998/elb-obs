"""Build the platform's fixed PMU sampler with a complete native OHOS SDK."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pmu_bundle import (
    BINARY_NAME, LEGACY_BINARY_NAME, MAX_BINARY_BYTES, SAMPLER_VERSION, SOURCE_PATH, TARGET, PmuBundleError,
    inspect_elf, sha256_bytes, sha256_file, verify_bundle,
)


BUILD_TIMEOUT_SECONDS = 120


def resolve_native_sdk(requested: Path | str | None = None) -> tuple[Path, Path]:
    value = str(requested or os.environ.get("HETERO_OPENHARMONY_NATIVE_SDK_ROOT", "")).strip()
    if not value:
        raise PmuBundleError("PMU_NATIVE_SDK_UNAVAILABLE", "请通过 --native-sdk 或 HETERO_OPENHARMONY_NATIVE_SDK_ROOT 指定完整 OpenHarmony native SDK。紧凑 SDK 头文件包不能构建 PMU 采集器。")
    root = Path(value).expanduser().resolve()
    if not (root / "sysroot").is_dir() and (root / "native").is_dir():
        root = root / "native"
    compiler = root / "llvm" / "bin" / ("clang.exe" if os.name == "nt" else "clang")
    required = (
        root / "sysroot" / "usr" / "include" / "linux" / "perf_event.h",
        root / "sysroot" / "usr" / "lib" / TARGET / "Scrt1.o",
        root / "sysroot" / "usr" / "lib" / TARGET / "libc.so",
    )
    if not compiler.is_file() or not os.access(compiler, os.X_OK) or any(not path.is_file() for path in required):
        raise PmuBundleError("PMU_NATIVE_SDK_UNAVAILABLE", "完整 native SDK 缺少宿主机 clang、PMU 头文件或 AArch64 sysroot/libc；请使用当前主机操作系统对应的 SDK。")
    return root, compiler


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    options: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    if os.name == "posix":
        options["start_new_session"] = True
    elif os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(command, **options)
    except OSError as exc:
        raise PmuBundleError("PMU_BUILD_FAILED", "无法启动 native SDK 编译器，请确认 SDK 与当前主机操作系统匹配。") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise PmuBundleError("PMU_BUILD_TIMEOUT", f"PMU 构建命令超过 {timeout} 秒，已终止所属编译进程。") from exc
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if result.returncode:
        raise PmuBundleError("PMU_BUILD_FAILED", "PMU 交叉编译失败：" + (stderr or stdout)[-4000:])
    return result


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _portable_build_identity(
    *,
    command: list[str],
    sdk: Path,
    compiler: Path,
    binary: Path,
    compiler_version: str,
) -> tuple[list[str], str, str]:
    """Remove checkout, SDK and temporary-directory paths from a receipt.

    The build still uses resolved absolute paths.  Only its published evidence
    is normalized, so a receipt can be copied into a Desktop package without
    exposing the build host or becoming tied to one checkout location.
    """
    portable_command: list[str] = []
    for item in command:
        if item == str(compiler):
            portable_command.append(compiler.name)
        elif item == str(SOURCE_PATH):
            portable_command.append(SOURCE_PATH.name)
        elif item == str(binary):
            portable_command.append(BINARY_NAME)
        else:
            portable_command.append(item.replace(str(sdk), "<OPENHARMONY_NATIVE_SDK>"))
    portable_version = compiler_version.replace(str(sdk), "<OPENHARMONY_NATIVE_SDK>")
    return portable_command, compiler.name, portable_version


def build_bundle(*, native_sdk: Path | str | None = None, output: Path) -> dict[str, Any]:
    sdk, compiler = resolve_native_sdk(native_sdk)
    compiler_hash = sha256_file(compiler)
    compiler_version = _run([str(compiler), "--version"], timeout=10).stdout.strip()[:4000]
    source_hash = sha256_file(SOURCE_PATH)
    root = Path(output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    versions = root / "versions"
    if versions.is_symlink():
        raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU versions 目录不能是符号链接。")
    versions.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pmu-build-", dir=root) as temporary_name:
        temporary = Path(temporary_name)
        staged = temporary / "version"
        staged.mkdir()
        binary = staged / BINARY_NAME
        command = [str(compiler), f"--target={TARGET}", f"--sysroot={sdk / 'sysroot'}", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", str(SOURCE_PATH), "-o", str(binary)]
        _run(command, timeout=BUILD_TIMEOUT_SECONDS)
        if source_hash != sha256_file(SOURCE_PATH) or compiler_hash != sha256_file(compiler):
            raise PmuBundleError("PMU_BUILD_INPUT_CHANGED", "PMU 源码或编译器在构建期间发生变化，请重新构建。")
        with binary.open("rb") as stream:
            data = stream.read(MAX_BINARY_BYTES + 1)
        elf = inspect_elf(data)
        digest = sha256_bytes(data)
        receipt_command, receipt_compiler, receipt_compiler_version = _portable_build_identity(
            command=command,
            sdk=sdk,
            compiler=compiler,
            binary=binary,
            compiler_version=compiler_version,
        )
        receipt = {
            "schema_version": "pmu_sampler_build/v1", "sampler_version": SAMPLER_VERSION,
            "target": TARGET, "source": SOURCE_PATH.name, "source_sha256": source_hash,
            "compiler": {"path": receipt_compiler, "sha256": compiler_hash, "version": receipt_compiler_version},
            "command": receipt_command, "binary_sha256": digest, "binary_bytes": len(data), "elf": elf,
            "built_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt_bytes = _json_bytes(receipt)
        (staged / "receipt.json").write_bytes(receipt_bytes)
        binary.chmod(0o755)
        target = versions / digest
        if target.is_symlink():
            raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU 版本目录不能是符号链接。")
        if target.exists():
            # An identical binary keeps its original build receipt. Refuse a
            # corrupt existing version instead of modifying files in use.
            old_binary = target / BINARY_NAME
            if not old_binary.exists() and not old_binary.is_symlink():
                old_binary = target / LEGACY_BINARY_NAME
            old_receipt = target / "receipt.json"
            if old_binary.is_symlink() or old_receipt.is_symlink() or not old_binary.is_file() or not old_receipt.is_file() or old_binary.read_bytes() != data:
                raise PmuBundleError("PMU_BUNDLE_HASH_MISMATCH", "已存在的 PMU 哈希目录不完整或损坏。")
            receipt_bytes = old_receipt.read_bytes()
        manifest = {
            "schema_version": "pmu_sampler_bundle/v1", "binary_sha256": digest,
            "binary": f"versions/{digest}/{BINARY_NAME}",
            "receipt": f"versions/{digest}/receipt.json", "receipt_sha256": sha256_bytes(receipt_bytes),
        }
        # Validate the candidate against exactly the same deployment reader
        # before atomically publishing the root manifest.
        check = temporary / "check"
        check.mkdir()
        check_versions = check / "versions" / digest
        check_versions.mkdir(parents=True)
        shutil.copyfile(binary, check_versions / BINARY_NAME)
        (check_versions / "receipt.json").write_bytes(receipt_bytes)
        (check / "manifest.json").write_bytes(_json_bytes(manifest))
        verify_bundle(check)
        if target.exists():
            # Add the canonical filename without touching the legacy binary
            # or its receipt. Validation above also covers the original
            # receipt before any mutation of a published version directory.
            installed_binary = target / BINARY_NAME
            if not installed_binary.exists():
                try:
                    os.link(binary, installed_binary)
                except FileExistsError:
                    if installed_binary.is_symlink() or installed_binary.read_bytes() != data:
                        raise PmuBundleError("PMU_BUNDLE_HASH_MISMATCH", "已存在的 PMU 哈希目录不完整或损坏。")
        else:
            os.rename(staged, target)
        candidate_manifest = temporary / "manifest.json"
        candidate_manifest.write_bytes(_json_bytes(manifest))
        os.replace(candidate_manifest, root / "manifest.json")
    return verify_bundle(root)


def main() -> int:
    parser = argparse.ArgumentParser(description="交叉编译并校验平台固定的 OpenHarmony PMU 采集器；不连接设备。")
    parser.add_argument("--native-sdk", help="完整 native SDK 目录或包含 native/ 的父目录")
    parser.add_argument("--output", type=Path, help="PMU bundle 目录，默认 runtime assets/pmu-sampler")
    args = parser.parse_args()
    if args.output is None:
        from backend.platform_paths import runtime_asset_root
        args.output = runtime_asset_root() / "pmu-sampler"
    try:
        result = build_bundle(native_sdk=args.native_sdk, output=args.output)
    except PmuBundleError as exc:
        print(json.dumps({"status": "blocked", "code": exc.code, "message": exc.message}, ensure_ascii=False, indent=2))
        return 2
    except OSError as exc:
        print(json.dumps({"status": "blocked", "code": "PMU_BUILD_IO_FAILED", "message": "PMU 构建文件读写失败：" + str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"status": "ready", **result}, default=str, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
