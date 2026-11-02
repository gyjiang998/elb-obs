#!/usr/bin/env python3
"""Build and publish the fixed AArch64/OpenHarmony calibration benchmark.

The published bundle is content addressed and is consumed by the Desktop
runtime.  The runtime never accepts an arbitrary executable path from the
renderer: only a binary whose source/build receipt is covered by manifest.json
can be deployed to a phone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


TARGET = "aarch64-linux-ohos"
SOURCE = Path(__file__).with_name("benchmark.c")
MAX_BINARY_BYTES = 4 * 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def normalize_existing_receipt(receipt_bytes: bytes, expected_sha256: str | None) -> bytes:
    """Accept an audited receipt verbatim or repair only Git's CRLF rewrite.

    The manifest is the authority for the canonical LF bytes.  This deliberately
    does not parse and re-serialize JSON: any change beyond CRLF -> LF remains a
    hash failure instead of silently acquiring a new identity.
    """
    normalized = receipt_bytes.replace(b"\r\n", b"\n")
    if normalized != receipt_bytes:
        if expected_sha256 and sha256_bytes(normalized) == expected_sha256:
            return normalized
        raise RuntimeError("已发布校准基准构建回执与清单哈希不一致。")
    if not expected_sha256 or sha256_bytes(receipt_bytes) != expected_sha256:
        raise RuntimeError("已发布校准基准构建回执与清单哈希不一致。")
    return receipt_bytes


def parse_receipt(receipt_bytes: bytes) -> dict:
    try:
        text = receipt_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("已发布校准基准构建回执不是有效 UTF-8。") from exc
    if text.startswith("\ufeff"):
        raise RuntimeError("已发布校准基准构建回执不允许 UTF-8 BOM。")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("已发布校准基准构建回执无法读取。") from exc
    if not isinstance(value, dict):
        raise RuntimeError("已发布校准基准构建回执必须是 JSON 对象。")
    return value


def existing_receipt_hash(output: Path, binary_hash: str, binary_name: str) -> str | None:
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    expected_binary = f"versions/{binary_hash}/{binary_name}"
    expected_receipt = f"versions/{binary_hash}/receipt.json"
    receipt_hash = manifest.get("receipt_sha256")
    if (manifest.get("schema_version") == "parallel_calibration_benchmark_bundle/v1"
            and manifest.get("binary") == expected_binary
            and manifest.get("binary_sha256") == binary_hash
            and manifest.get("receipt") == expected_receipt
            and isinstance(receipt_hash, str) and len(receipt_hash) == 64):
        return receipt_hash
    return None


def native_sdk_root(value: str | None) -> Path:
    raw = str(value or os.environ.get("HETERO_OPENHARMONY_NATIVE_SDK_ROOT") or "").strip()
    if not raw:
        raise ValueError("请用 --native-sdk 指定完整 OpenHarmony native SDK。")
    root = Path(raw).expanduser().resolve()
    if not (root / "sysroot").is_dir() and (root / "native" / "sysroot").is_dir():
        root = root / "native"
    compiler = root / "llvm" / "bin" / ("clang.exe" if os.name == "nt" else "clang")
    if not compiler.is_file() or not (root / "sysroot").is_dir():
        raise ValueError("native SDK 缺少当前主机可执行的 clang 或 sysroot。")
    return root


def build(*, sdk: Path, output: Path) -> dict:
    compiler = sdk / "llvm" / "bin" / ("clang.exe" if os.name == "nt" else "clang")
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    versions = output / "versions"
    versions.mkdir(exist_ok=True)
    source_hash = sha256(SOURCE)
    compiler_hash = sha256(compiler)
    compiler_version = subprocess.run(
        [str(compiler), "--version"], check=True, capture_output=True, text=True, timeout=15,
    ).stdout.strip()[:4000].replace(str(sdk), "<OPENHARMONY_NATIVE_SDK>")
    with tempfile.TemporaryDirectory(prefix=".calibration-build-", dir=output) as temporary_name:
        temporary = Path(temporary_name)
        binary = temporary / "parallel_calibration_benchmark"
        command = [
            str(compiler), f"--target={TARGET}", f"--sysroot={sdk / 'sysroot'}",
            "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
            str(SOURCE), "-o", str(binary),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if completed.returncode or not binary.is_file():
            raise RuntimeError("校准基准交叉编译失败：" + (completed.stderr or completed.stdout)[-4000:])
        if binary.stat().st_size < 64 or binary.stat().st_size > MAX_BINARY_BYTES:
            raise RuntimeError("校准基准产物大小无效。")
        header = binary.read_bytes()[:20]
        if header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1 or int.from_bytes(header[18:20], "little") != 183:
            raise RuntimeError("校准基准不是 AArch64 little-endian ELF64。")
        binary_hash = sha256(binary)
        version = versions / binary_hash
        receipt_command = []
        for item in command:
            if item in (str(compiler), str(SOURCE), str(binary)):
                receipt_command.append(Path(item).name)
            else:
                receipt_command.append(item.replace(str(sdk), "<OPENHARMONY_NATIVE_SDK>"))
        receipt = {
            "schema_version": "parallel_calibration_benchmark_build/v1",
            "target": TARGET,
            "source": SOURCE.name,
            "source_sha256": source_hash,
            "compiler": {"name": compiler.name, "sha256": compiler_hash, "version": compiler_version},
            "command": receipt_command,
            "binary_sha256": binary_hash,
            "binary_bytes": binary.stat().st_size,
            "built_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt_bytes = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        if not version.exists():
            version.mkdir()
            shutil.copyfile(binary, version / binary.name)
            (version / binary.name).chmod(0o755)
            (version / "receipt.json").write_bytes(receipt_bytes)
        else:
            published_binary = version / binary.name
            published_receipt = version / "receipt.json"
            if (not published_binary.is_file() or sha256(published_binary) != binary_hash
                    or not published_receipt.is_file()):
                raise RuntimeError("同哈希校准基准目录不完整或内容不一致。")
            # The directory is addressed by the binary hash.  Rebuilding an
            # identical binary must keep its first audited receipt; otherwise
            # manifest.json could advertise a receipt hash that was never
            # written to disk.
            receipt_bytes_on_disk = published_receipt.read_bytes()
            expected_receipt_hash = existing_receipt_hash(output, binary_hash, binary.name)
            if expected_receipt_hash or b"\r\n" in receipt_bytes_on_disk:
                receipt_bytes = normalize_existing_receipt(receipt_bytes_on_disk, expected_receipt_hash)
            else:
                receipt_bytes = receipt_bytes_on_disk
            if receipt_bytes != receipt_bytes_on_disk:
                normalized_receipt = temporary / "receipt.json"
                normalized_receipt.write_bytes(receipt_bytes)
                os.replace(normalized_receipt, published_receipt)
            receipt_record = parse_receipt(receipt_bytes)
            if (receipt_record.get("schema_version") != "parallel_calibration_benchmark_build/v1"
                    or receipt_record.get("target") != TARGET
                    or receipt_record.get("binary_sha256") != binary_hash
                    or receipt_record.get("binary_bytes") != binary.stat().st_size):
                raise RuntimeError("已发布校准基准构建回执与二进制身份不一致。")
        manifest = {
            "schema_version": "parallel_calibration_benchmark_bundle/v1",
            "binary": f"versions/{binary_hash}/{binary.name}",
            "binary_sha256": binary_hash,
            "binary_bytes": binary.stat().st_size,
            "receipt": f"versions/{binary_hash}/receipt.json",
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        }
        candidate = temporary / "manifest.json"
        candidate.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
        os.replace(candidate, output / "manifest.json")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="构建固定 OpenHarmony 校准微基准 bundle。")
    parser.add_argument("--native-sdk")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("prebuilt") / TARGET)
    args = parser.parse_args()
    try:
        result = build(sdk=native_sdk_root(args.native_sdk), output=args.output)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "blocked", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"status": "ready", **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
