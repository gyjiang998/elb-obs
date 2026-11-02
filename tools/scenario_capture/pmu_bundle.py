"""Validate the fixed OpenHarmony PMU sampler without executing target code."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path, PurePosixPath
from typing import Any


SOURCE_PATH = Path(__file__).resolve().with_name("thread_pmu_sampler.c")
LEGACY_SOURCE_NAME = "gallery_thread_pmu.c"
SAMPLER_VERSION = "1.2.0"
TARGET = "aarch64-linux-ohos"
BINARY_NAME = "thread_pmu_sampler"
LEGACY_BINARY_NAME = "gallery_thread_pmu_focus"
INTERPRETER = "/lib/ld-musl-aarch64.so.1"
MAX_BINARY_BYTES = 16 * 1024 * 1024
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PmuBundleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_elf(data: bytes) -> dict[str, Any]:
    """Require an executable AArch64 ELF using the OpenHarmony musl loader."""
    def invalid(reason: str) -> None:
        raise PmuBundleError("PMU_BUNDLE_ELF_INVALID", "PMU 产物不是有效的 AArch64 OpenHarmony 可执行文件：" + reason)

    if len(data) < 64 or len(data) > MAX_BINARY_BYTES:
        invalid("文件大小异常")
    if data[:7] != b"\x7fELF\x02\x01\x01":
        invalid("需要 ELF64、小端、ELF version 1")
    fields = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    kind, machine, version, entry, phoff, _, _, ehsize, phsize, phnum, _, _, _ = fields
    if kind not in (2, 3) or machine != 183 or version != 1 or not entry:
        invalid("需要 AArch64 EXEC/PIE 和有效入口")
    if ehsize != 64 or phsize != 56 or not 1 <= phnum <= 128 or phoff < 64 or phoff + phsize * phnum > len(data):
        invalid("program header 越界")
    interpreters: list[str] = []
    load_segments = 0
    executable_entry = False
    for index in range(phnum):
        segment_type, flags, offset, address, _, file_size, memory_size, _ = struct.unpack_from("<IIQQQQQQ", data, phoff + index * phsize)
        if file_size and (offset > len(data) or file_size > len(data) - offset):
            invalid("segment 越界")
        if segment_type == 1:
            load_segments += 1
            if file_size > memory_size:
                invalid("LOAD segment 长度异常")
            executable_entry |= bool(flags & 1 and address <= entry < address + file_size)
        if segment_type == 3:
            raw = data[offset:offset + file_size]
            if not 2 <= len(raw) <= 256 or raw[-1:] != b"\0" or b"\0" in raw[:-1]:
                invalid("interpreter 格式异常")
            try:
                interpreters.append(raw[:-1].decode("ascii"))
            except UnicodeDecodeError:
                invalid("interpreter 编码异常")
    if not load_segments or not executable_entry or interpreters != [INTERPRETER]:
        invalid("需要唯一的 OpenHarmony musl interpreter")
    return {"class": "ELF64", "endianness": "little", "machine": "AArch64", "interpreter": INTERPRETER}


def _safe_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative:
        raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU 资产路径无效。")
    rel = PurePosixPath(relative)
    if not relative or rel.is_absolute() or any(part in ("", ".", "..") for part in relative.split("/")):
        raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU 资产路径越出 bundle。")
    candidate = root
    for part in rel.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU bundle 内不允许符号链接。")
    if not candidate.is_file() or not candidate.resolve().is_relative_to(root):
        raise PmuBundleError("PMU_BUNDLE_UNAVAILABLE", "PMU 资产文件缺失：" + relative)
    return candidate


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        if path.stat().st_size > 128 * 1024:
            raise ValueError("metadata too large")
        with path.open("rb") as stream:
            data = stream.read(128 * 1024 + 1)
        if len(data) > 128 * 1024:
            raise ValueError("metadata too large")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("metadata must be an object")
        return value, data
    except (OSError, ValueError, UnicodeError) as exc:
        raise PmuBundleError("PMU_BUNDLE_MANIFEST_INVALID", "PMU 构建清单无法读取。") from exc


def verify_bundle(bundle_root: Path) -> dict[str, Any]:
    """Verify manifest, receipt, current source binding, binary hash and target ABI.

    The compiler and SDK need not exist on a deployment machine. The receipt
    records their build-time identity; target bytes are never executed here.
    """
    try:
        return _verify_bundle(bundle_root)
    except OSError as exc:
        raise PmuBundleError("PMU_BUNDLE_UNAVAILABLE", "PMU bundle 无法读取，请检查资产文件与权限。") from exc


def _verify_bundle(bundle_root: Path) -> dict[str, Any]:
    root = Path(bundle_root).expanduser().resolve()
    manifest_path = _safe_file(root, "manifest.json")
    manifest, _ = _read_json(manifest_path)
    if manifest.get("schema_version") != "pmu_sampler_bundle/v1":
        raise PmuBundleError("PMU_BUNDLE_MANIFEST_INVALID", "PMU bundle 清单版本不兼容。")
    digest = manifest.get("binary_sha256")
    receipt_digest = manifest.get("receipt_sha256")
    if not isinstance(digest, str) or not HEX_SHA256.fullmatch(digest) or not isinstance(receipt_digest, str) or not HEX_SHA256.fullmatch(receipt_digest):
        raise PmuBundleError("PMU_BUNDLE_MANIFEST_INVALID", "PMU 构建清单缺少有效哈希。")
    expected_binary = manifest.get("binary")
    accepted_binaries = {f"versions/{digest}/{name}" for name in (BINARY_NAME, LEGACY_BINARY_NAME)}
    expected_receipt = f"versions/{digest}/receipt.json"
    if not isinstance(expected_binary, str) or expected_binary not in accepted_binaries or manifest.get("receipt") != expected_receipt:
        raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU 清单必须指向对应哈希的固定版本目录。")
    receipt_path = _safe_file(root, expected_receipt)
    receipt, receipt_bytes = _read_json(receipt_path)
    if sha256_bytes(receipt_bytes) != receipt_digest:
        raise PmuBundleError("PMU_BUNDLE_HASH_MISMATCH", "PMU 构建凭据哈希不匹配。")
    if receipt.get("schema_version") != "pmu_sampler_build/v1" or receipt.get("sampler_version") != SAMPLER_VERSION or receipt.get("target") != TARGET:
        raise PmuBundleError("PMU_BUNDLE_MANIFEST_INVALID", "PMU 构建版本或目标不匹配。")
    source_digest = sha256_file(SOURCE_PATH)
    # 1.1.0 was originally shipped under a Gallery filename. The canonical
    # source is byte-identical; accept that filename only with today's exact
    # source hash, target, receipt and executable checks unchanged.
    if receipt.get("source") not in (SOURCE_PATH.name, LEGACY_SOURCE_NAME) or receipt.get("source_sha256") != source_digest:
        raise PmuBundleError("PMU_BUNDLE_SOURCE_MISMATCH", "PMU 产物与当前平台采集器源码不匹配，请重新构建。")
    compiler = receipt.get("compiler")
    command = receipt.get("command")
    if not isinstance(compiler, dict) or not isinstance(compiler.get("sha256"), str) or not HEX_SHA256.fullmatch(compiler["sha256"]) or not isinstance(compiler.get("path"), str) or not compiler["path"] or not isinstance(compiler.get("version"), str):
        raise PmuBundleError("PMU_BUNDLE_MANIFEST_INVALID", "PMU 构建凭据缺少编译器身份。")
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command) or not command or command[0] != compiler["path"] or f"--target={TARGET}" not in command or "-std=c11" not in command or "-O2" not in command or "-Werror" not in command or not any(part.startswith("--sysroot=") for part in command):
        raise PmuBundleError("PMU_BUNDLE_MANIFEST_INVALID", "PMU 构建命令缺少固定目标与构建参数。")
    binary_path = _safe_file(root, expected_binary)
    try:
        if binary_path.stat().st_size > MAX_BINARY_BYTES:
            raise PmuBundleError("PMU_BUNDLE_ELF_INVALID", "PMU 二进制超过大小上限。")
        with binary_path.open("rb") as stream:
            data = stream.read(MAX_BINARY_BYTES + 1)
    except OSError as exc:
        raise PmuBundleError("PMU_BUNDLE_UNAVAILABLE", "PMU 二进制无法读取。") from exc
    if sha256_bytes(data) != digest or receipt.get("binary_sha256") != digest or receipt.get("binary_bytes") != len(data):
        raise PmuBundleError("PMU_BUNDLE_HASH_MISMATCH", "PMU 二进制哈希或大小与构建凭据不匹配。")
    elf = inspect_elf(data)
    return {
        "binary_path": binary_path, "sha256": digest, "binary_sha256": digest,
        "binary_bytes": len(data), "source_sha256": source_digest,
        "sampler_version": SAMPLER_VERSION, "target": TARGET,
        "manifest_path": manifest_path, "receipt_path": receipt_path, "receipt": receipt, "elf": elf,
    }
