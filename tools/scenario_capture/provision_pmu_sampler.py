"""Install the repository's verified PMU bundle; never compile or run target code."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .pmu_bundle import (
    BINARY_NAME, HEX_SHA256, LEGACY_BINARY_NAME, MAX_BINARY_BYTES, PmuBundleError,
    sha256_bytes, verify_bundle,
)


BUNDLED_ROOT = Path(__file__).resolve().parent / "prebuilt" / "aarch64-linux-ohos"


def _root(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    # Parent directories are administrator-selected (and may include /tmp on
    # macOS). The managed bundle itself and everything below it must be real.
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU bundle 必须是普通目录，不能是符号链接。")
    return path.parent.resolve() / path.name


def _check_destination(root: Path) -> None:
    for relative in ("manifest.json", "versions"):
        path = root / relative
        if path.is_symlink():
            raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU 安装目标中不允许符号链接：" + relative)
    if (root / "manifest.json").exists() and not (root / "manifest.json").is_file():
        raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU manifest.json 不是普通文件。")
    if (root / "manifest.json").is_file():
        try:
            manifest = json.loads(_read_bytes(root / "manifest.json", 128 * 1024))
        except (ValueError, PmuBundleError):
            # A damaged PMU manifest can be recovered when the directory is
            # otherwise recognized; a readable unrelated manifest cannot.
            manifest = None
        if manifest is not None and (not isinstance(manifest, dict) or manifest.get("schema_version") != "pmu_sampler_bundle/v1"):
            raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU 安装目标包含其他类型的 manifest.json，请指定独立的 pmu-sampler 目录。")
    if (root / "versions").exists() and not (root / "versions").is_dir():
        raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU versions 不是普通目录。")
    if root.exists() and any(root.iterdir()) and not any((root / name).exists() for name in ("manifest.json", "versions")):
        raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU 安装目标是非空的非 bundle 目录，请指定独立的 pmu-sampler 目录。")


def _result(status: str, root: Path, verified: dict[str, Any]) -> dict[str, Any]:
    return {"status": status, "bundle_root": str(root), **{
        key: verified[key] for key in
        ("binary_sha256", "source_sha256", "sampler_version", "target", "binary_bytes")
    }}


def _read_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise PmuBundleError("PMU_BUNDLE_MANIFEST_INVALID", "PMU 资产文件超过大小上限。")
    return data


def _write_snapshot(root: Path, manifest_bytes: bytes, binary_bytes: bytes, receipt_bytes: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        digest = manifest["binary_sha256"]
        if not isinstance(digest, str) or not HEX_SHA256.fullmatch(digest):
            raise ValueError("invalid digest")
        if manifest.get("binary") not in {f"versions/{digest}/{name}" for name in (BINARY_NAME, LEGACY_BINARY_NAME)} or manifest.get("receipt") != f"versions/{digest}/receipt.json":
            raise ValueError("invalid version path")
    except (ValueError, TypeError, KeyError) as exc:
        raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU 资产快照清单或路径无效。") from exc
    version = root / "versions" / digest
    version.mkdir(parents=True)
    binary = version / Path(manifest["binary"]).name
    binary.write_bytes(binary_bytes)
    binary.chmod(0o755)
    (version / "receipt.json").write_bytes(receipt_bytes)
    (root / "manifest.json").write_bytes(manifest_bytes)
    return verify_bundle(root)


def provision_bundle(output: Path, source: Path = BUNDLED_ROOT) -> dict[str, Any]:
    """Reuse a valid deployment, or atomically activate a verified bundled version.

    Existing version directories are immutable. Invalid same-hash directories
    are reported instead of repaired in place; unrelated versions are kept.
    """
    root = _root(output)
    _check_destination(root)
    try:
        return _result("reused", root, verify_bundle(root))
    except PmuBundleError:
        pass

    bundled = verify_bundle(_root(source))
    manifest_bytes = _read_bytes(bundled["manifest_path"], 128 * 1024)
    binary_bytes = _read_bytes(bundled["binary_path"], MAX_BINARY_BYTES)
    receipt_bytes = _read_bytes(bundled["receipt_path"], 128 * 1024)
    # Check a byte snapshot before touching the destination, including races
    # with source updates between verification and reading these three files.
    with tempfile.TemporaryDirectory(prefix="hetero-pmu-verify-") as temporary:
        checked = _write_snapshot(Path(temporary), manifest_bytes, binary_bytes, receipt_bytes)
        digest = checked["binary_sha256"]
        manifest = json.loads(manifest_bytes)
        target = root / "versions" / digest
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise PmuBundleError("PMU_BUNDLE_PATH_INVALID", "PMU 哈希版本目录不能是符号链接或普通文件。")
        target_existed = target.exists()
        if target_existed:
            # A previous deployment may have a different valid build receipt
            # or the historical executable filename. Reuse those exact files.
            binary = target / BINARY_NAME
            if not binary.exists() and not binary.is_symlink():
                binary = target / LEGACY_BINARY_NAME
            receipt = target / "receipt.json"
            try:
                if binary.is_symlink() or receipt.is_symlink() or not binary.is_file() or not receipt.is_file():
                    raise ValueError("missing or linked files")
                existing_binary = _read_bytes(binary, MAX_BINARY_BYTES)
                existing_receipt = _read_bytes(receipt, 128 * 1024)
                manifest["binary"] = f"versions/{digest}/{binary.name}"
                manifest["receipt_sha256"] = sha256_bytes(existing_receipt)
                manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode() + b"\n"
                with tempfile.TemporaryDirectory(prefix="hetero-pmu-existing-") as check:
                    _write_snapshot(Path(check), manifest_bytes, existing_binary, existing_receipt)
            except (PmuBundleError, ValueError, OSError) as exc:
                raise PmuBundleError("PMU_PROVISION_VERSION_CONFLICT", "已存在的 PMU 同哈希版本目录损坏或不匹配；保留原文件，请检查后移走该版本目录再部署。") from exc

        # All input/target validation has completed. Stage on the destination
        # filesystem so both version publication and manifest replacement are
        # atomic; the old manifest remains active until the final replacement.
        root.mkdir(parents=True, exist_ok=True)
        _check_destination(root)
        versions = root / "versions"
        versions.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".pmu-provision-", dir=root) as staging:
            staged = Path(staging)
            if not target_existed:
                if target.exists() or target.is_symlink():
                    raise PmuBundleError("PMU_PROVISION_VERSION_CONFLICT", "PMU 版本目录在安装期间发生变化，请重新部署。")
                stage_version = staged / digest
                shutil.copytree(Path(temporary) / "versions" / digest, stage_version)
                # Refuse an intervening directory/link rather than overwrite
                # files that may already be used by a sampler deployment.
                if target.exists() or target.is_symlink():
                    raise PmuBundleError("PMU_PROVISION_VERSION_CONFLICT", "PMU 版本目录在安装期间发生变化，请重新部署。")
                os.rename(stage_version, target)
            staged_manifest = staged / "manifest.json"
            staged_manifest.write_bytes(manifest_bytes)
            os.replace(staged_manifest, root / "manifest.json")
    return _result("installed", root, verify_bundle(root))


def main() -> int:
    parser = argparse.ArgumentParser(description="校验并安装仓库内置 PMU 采样器，不需要 Native SDK，也不连接设备。")
    parser.add_argument("--output", type=Path, help="PMU bundle 目录，默认环境配置或 runtime assets/pmu-sampler")
    args = parser.parse_args()
    if args.output is None:
        from backend.platform_paths import runtime_asset_root
        configured = os.environ.get("HETERO_PMU_SAMPLER_BUNDLE_ROOT", "").strip()
        args.output = Path(configured) if configured else runtime_asset_root() / "pmu-sampler"
    try:
        result = provision_bundle(args.output)
    except PmuBundleError as exc:
        result = {"status": "blocked", "code": exc.code, "message": exc.message}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    except OSError as exc:
        print(json.dumps({"status": "blocked", "code": "PMU_PROVISION_IO_FAILED", "message": "PMU 资产部署失败：" + str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
